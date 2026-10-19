"""IG 食譜帳號圖片批次處理：統一裁成橫向 4:3（預設一律橫幅，不管原圖是直幅還是
橫幅拍的）+ 統一的食物攝影風格濾鏡。

子命令：
    list-new     列出 input/ 裡還沒記錄在 output/manifest.json 的新照片
    auto-detect  自動偵測照片主體，算出建議的 offset-x/offset-y/zoom/rotate
    preview      產生裁切+濾鏡後的預覽圖，存到 .cache/previews/，給人工確認用
    finalize     套用同一套邏輯，存到 output/<菜名>.jpg，並更新 manifest.json

用法範例：
    python scripts/process_photo.py list-new
    python scripts/process_photo.py auto-detect input/beef.jpg
    python scripts/process_photo.py preview input/beef.jpg --offset-y -0.1 --zoom 1.2
    python scripts/process_photo.py finalize input/beef.jpg --name "紅燒牛肉" --offset-y -0.1 --zoom 1.2
"""

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_ROOT / "input"
OUTPUT_DIR = PROJECT_ROOT / "output"
PREVIEW_DIR = PROJECT_ROOT / ".cache" / "previews"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"

# 不允許出現在 Windows 檔名裡的字元
_INVALID_FILENAME_CHARS = '<>:"/\\|?*'

# 統一輸出比例 4:3，預設一律橫幅（orientation="landscape"）。直幅原圖也會被裁成
# 橫幅——這是刻意的，使用者要求所有貼文統一橫向 4:3。長邊固定 1440px，維持不錯的畫質。
RATIO_LONG, RATIO_SHORT = 4, 3
OUTPUT_LONG_SIDE = 1440

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}

# 固定的風格參數：參考一般食物攝影 IG 帳號常見的做法——真實但有質感、
# 飽和度其實是「降一點」而不是推更高、清晰度/對比放輕、暖色調幅度很小、
# 加一點點顆粒感維持自然感，不要厚重濾鏡。所有照片套同一組，保證風格一致。
CONTRAST_FACTOR = 1.0
SATURATION_FACTOR = 0.92
BRIGHTNESS_FACTOR = 1.02
WARM_R_FACTOR = 1.015
WARM_B_FACTOR = 0.99
VIGNETTE_STRENGTH = 0.05
GRAIN_STRENGTH = 6  # 輕微顆粒感，0 關閉
SHADOW_LIFT = 16  # 陰影提亮：避免暗部被壓成死黑，0 關閉
SHADOW_COMPRESS = 0.94  # 配合 SHADOW_LIFT 做的輕微壓縮，讓整體色階不過曝

AUTO_MARGIN = 1.18  # auto-detect 偵測到主體後，裁切框比主體再大 18%，留一點呼吸空間


def _load_manifest() -> dict:
    """manifest.json 記錄 input 檔名（不含副檔名）對應到 output 裡實際用的菜名檔名，
    讓 output 可以用菜名命名，同時 list-new 還是能正確判斷哪些 input 已經處理過。"""
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {}


def _save_manifest(manifest: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _sanitize_filename(name: str) -> str:
    cleaned = "".join(c for c in name if c not in _INVALID_FILENAME_CHARS).strip()
    return cleaned or "untitled"


def _load_image(path: Path) -> Image.Image:
    image = Image.open(path)
    image = ImageOps.exif_transpose(image)  # 修正手機照片常見的 EXIF 旋轉
    return image.convert("RGB")


def _detect_circle_box(gray, sw: int, sh: int) -> tuple[float, float, float, float] | None:
    """用 Hough 圓形偵測找圓盤/碗的邊界。食物本身（尤其油亮、反光的滷汁類）常常
    會形成誤導的假圓形邊界，這個方法單獨看容易被騙，呼叫端要跟 _detect_bbox_box
    的結果交叉比對，不能直接當作最終答案。"""
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=sw // 2,
        param1=100,
        param2=40,
        minRadius=int(min(sw, sh) * 0.25),
        maxRadius=int(min(sw, sh) * 0.48),  # 直徑不能超過畫面較短的那一邊，不然圓根本塞不進畫面裡
    )
    if circles is None:
        return None
    candidates = [c for c in circles[0] if c[2] <= min(sw, sh) * 0.5]  # 防呆：濾掉幾何上塞不下的圓
    if not candidates:
        return None
    cx0, cy0 = sw / 2, sh / 2
    # 盤子/碗通常在畫面中間偏下一點，用「離中心近 + 半徑大」當分數選最可能的那個圓
    best = max(candidates, key=lambda c: c[2] - 0.3 * math.hypot(c[0] - cx0, c[1] - cy0))
    cx, cy, r = best
    return (cx / sw, cy / sh, r / sw, r / sh)


def _detect_bbox_box(gray, small, sw: int, sh: int) -> tuple[float, float, float, float] | None:
    """用「飽和度 + 邊緣對比」找出跟木紋桌面對比最明顯的區域當主體範圍。不會被
    單一反光假圓形騙到，但邊界沒有圓形偵測精準，主要拿來跟圓形偵測交叉驗證，
    也是圓形偵測失敗時（例如粽子、方盤這類不是正圓的主體）的退路。"""
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1].astype("float32")
    edges = cv2.Canny(gray, 50, 150).astype("float32")
    interest = saturation / 255 + edges / 255
    threshold = np.percentile(interest, 70)
    mask = (interest >= threshold).astype("uint8") * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), "uint8"))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < sw * sh * 0.04:
        return None  # 太小，大概是雜訊不是真的主體
    x, y, w, h = cv2.boundingRect(largest)
    cx, cy = x + w / 2, y + h / 2
    return (cx / sw, cy / sh, (w / 2) / sw, (h / 2) / sh)


def _detect_subject_box(image: Image.Image) -> tuple[float, float, float, float] | None:
    """自動偵測畫面主體（通常是圓盤/碗）大概的範圍。回傳 (cx, cy, half_w, half_h)，
    四個數字都是相對原圖寬高的比例（0~1）。偵測不到就回傳 None，呼叫端要有
    退回手動模式的準備，不能假設一定偵測得到。

    同時跑圓形偵測與範圍偵測，兩者中心點夠接近（代表彼此驗證通過）才用精準度
    較高的圓形結果；兩者差太多時，圓形偵測多半是被食物表面的反光或紋理騙到，
    改採信比較不會誤判、但邊界較鬆的範圍偵測結果。"""
    if cv2 is None:
        return None

    width, height = image.size
    array = np.array(image)[:, :, ::-1]  # RGB -> BGR
    scale = 800 / max(width, height)
    small = cv2.resize(array, (max(1, round(width * scale)), max(1, round(height * scale))))
    sh, sw = small.shape[:2]

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 5)

    circle_box = _detect_circle_box(gray, sw, sh)
    bbox_box = _detect_bbox_box(gray, small, sw, sh)

    if circle_box and bbox_box:
        dx = abs(circle_box[0] - bbox_box[0])
        dy = abs(circle_box[1] - bbox_box[1])
        if dx < 0.08 and dy < 0.08:
            return circle_box  # 兩種方法對中心點的判斷一致，採用邊界較精準的圓形結果
        return bbox_box  # 差太多，圓形偵測不可信（常是反光/紋理造成的假圓），改用範圍偵測

    return circle_box or bbox_box


def _auto_fit_params(
    image: Image.Image, orientation: str, margin: float = AUTO_MARGIN
) -> tuple[float, float, float] | None:
    """給定一個（可能已經旋轉過的）圖和目標方向，偵測主體後算出能讓主體置中、
    周圍留 margin 倍空間的 offset_x/offset_y/zoom。偵測失敗回傳 None。"""
    box = _detect_subject_box(image)
    if box is None:
        return None
    cx, cy, half_w, half_h = box
    if half_w > 0.5 or half_h > 0.5:
        return None  # 偵測到的範圍比整張圖還大，幾何上不合理，當作偵測失敗
    width, height = image.size

    if orientation == "landscape":
        ratio_w, ratio_h = RATIO_LONG, RATIO_SHORT
    else:
        ratio_w, ratio_h = RATIO_SHORT, RATIO_LONG

    subject_w = half_w * 2 * width
    subject_h = half_h * 2 * height
    # 需要的裁切框至少要蓋過主體（含 margin），裁切框本身維持目標比例
    need_w = subject_w * margin
    need_h = subject_h * margin
    if need_w / need_h > ratio_w / ratio_h:
        crop_w = need_w
        crop_h = need_w * ratio_h / ratio_w
    else:
        crop_h = need_h
        crop_w = need_h * ratio_w / ratio_h

    base_w = min(width, height * ratio_w / ratio_h) if width / height > ratio_w / ratio_h else width
    base_h = base_w * ratio_h / ratio_w if width / height > ratio_w / ratio_h else height
    # 跟「原圖能裁到的最大範圍」比較，算出對應的 zoom（zoom 愈大代表裁得愈緊）
    zoom = max(1.0, min(base_w / crop_w, base_h / crop_h))

    offset_x = (cx * width - width / 2) / width
    offset_y = (cy * height - height / 2) / height
    return (round(float(offset_x), 3), round(float(offset_y), 3), round(float(zoom), 3))


def _detect_tilt_angle(image: Image.Image) -> float | None:
    """圓盤本身是圓的，單靠圓形邊界看不出鏡頭有沒有歪（圓怎麼轉都還是圓），
    所以改用畫面裡的直線參考物（桌緣、木紋、餐墊邊緣）來判斷水平基準在哪。
    用 Hough 直線偵測找出大致水平的線，算出中位數角度當作校正角度；找不到
    足夠多、角度又夠一致的線就回傳 None——沒把握就不要亂猜著硬轉。"""
    if cv2 is None:
        return None

    width, height = image.size
    array = np.array(image)[:, :, ::-1]
    scale = 800 / max(width, height)
    small = cv2.resize(array, (max(1, round(width * scale)), max(1, round(height * scale))))
    sh, sw = small.shape[:2]

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=80, minLineLength=int(min(sw, sh) * 0.35), maxLineGap=10
    )
    if lines is None:
        return None

    angles = []
    for line in lines.reshape(-1, 4):
        x1, y1, x2, y2 = line
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
        # 收斂到 -45~45 度，代表「大致水平」的線；太陡（接近垂直）的線不是桌緣參考線，不採計
        normalized = ((angle + 45) % 90) - 45
        if abs(normalized) < 20:
            angles.append(normalized)

    if len(angles) < 4:
        return None  # 可信的水平線太少，不要用猜的

    angles_arr = np.array(angles)
    if float(np.std(angles_arr)) > 4.0:
        return None  # 各條線角度差太多，代表偵測到的不是同一個水平基準，信心不足

    median_angle = float(np.median(angles_arr))
    return median_angle if abs(median_angle) >= 0.8 else 0.0  # 小於 0.8 度的傾斜不值得修正


def auto_detect_params(input_path: Path) -> dict | None:
    """自動幫一張照片決定 offset_x/offset_y/zoom/rotate。分兩步：
    1) 先找畫面裡的水平參考線，算出鏡頭本身歪了幾度（deskew），不確定就當作 0。
    2) 在這個已經校正過水平的基礎上，分別試「不轉」「轉 90 度」「轉 -90（270）度」
       三種方向偵測主體、試算需要的 zoom，選 zoom 最小（代表主體跟目標比例最合、
       不用裁太緊）的那個方向，而不是只固定往同一個方向轉——避免每次都往同一個
       方向轉導致某些照片的上下左右跑錯邊。
    三種方向都偵測不到主體的話回傳 None，呼叫端要退回手動模式。"""
    image = _load_image(input_path)
    tilt = _detect_tilt_angle(image) or 0.0

    candidates = []
    for extra in (0, 90, 270):
        total_rotate = tilt + extra
        test_image = image.rotate(total_rotate, expand=True) if abs(total_rotate) > 0.01 else image
        fit = _auto_fit_params(test_image, "landscape")
        if fit:
            candidates.append((total_rotate, fit))

    if not candidates:
        return None

    rotate, (offset_x, offset_y, zoom) = min(candidates, key=lambda c: c[1][2])
    return {
        "rotate": round(rotate, 2),
        "offset_x": offset_x,
        "offset_y": offset_y,
        "zoom": zoom,
        "deskew_degrees": round(tilt, 2),
    }


def compute_crop_box(
    width: int, height: int, offset_x: float, offset_y: float, zoom: float, orientation: str
) -> tuple[int, int, int, int]:
    """算出置中的 4:3（或直幅 3:4）裁切框，盡量用原圖最大範圍、只裁掉多餘的部分。
    offset 是相對圖片寬高的比例，zoom >= 1 代表裁切框再縮小、主體放大。orientation
    是 "landscape" 或 "portrait"——預設依原圖寬高自動判斷，但可以依照料理擺放的
    構圖邏輯覆寫（例如直幅拍的照片，若主體是橫向排列，裁成橫幅 4:3 可能更好看）。"""
    if orientation == "landscape":
        ratio_w, ratio_h = RATIO_LONG, RATIO_SHORT
    else:
        ratio_w, ratio_h = RATIO_SHORT, RATIO_LONG

    # 先找出貼合原圖、最大的目標比例裁切框（可能是裁寬或裁高，取較小者）
    if width / height > ratio_w / ratio_h:
        base_h = height
        base_w = height * ratio_w / ratio_h
    else:
        base_w = width
        base_h = width * ratio_h / ratio_w

    zoom = max(zoom, 1.0)
    crop_w = base_w / zoom
    crop_h = base_h / zoom

    cx = width / 2 + offset_x * width
    cy = height / 2 + offset_y * height

    left = cx - crop_w / 2
    top = cy - crop_h / 2
    left = max(0, min(left, width - crop_w))
    top = max(0, min(top, height - crop_h))

    return (round(left), round(top), round(left + crop_w), round(top + crop_h))


def apply_style(
    image: Image.Image, warmth: float = 1.0, sharpen: float = 1.0, brightness: float = 1.0
) -> Image.Image:
    """固定的食物攝影風格：先做溫和的陰影提亮（避免暗部死黑），再套用淡淡的
    暖色調、對比/飽和度微調、淡暗角與輕微銳化，整體走自然、不厚重的路線。這組
    參數是固定的，目的是讓每一張照片風格一致。

    warmth/sharpen/brightness 是例外：預設 1.0 代表用全域參數，單張照片有特殊
    狀況（光線偏冷、近距離特寫銳化太明顯有雜訊感、過曝太亮）時可以個別調整，不
    影響對比/飽和度等其他固定參數——不要為了遷就單一張照片就去改全域常數，除非
    使用者明確說要套用到所有照片。"""
    if SHADOW_LIFT:
        image = _lift_shadows(image, SHADOW_LIFT, SHADOW_COMPRESS)

    image = ImageEnhance.Contrast(image).enhance(CONTRAST_FACTOR)
    image = ImageEnhance.Color(image).enhance(SATURATION_FACTOR)
    image = ImageEnhance.Brightness(image).enhance(BRIGHTNESS_FACTOR * brightness)

    warm_r = 1 + (WARM_R_FACTOR - 1) * warmth
    warm_b = 1 - (1 - WARM_B_FACTOR) * warmth
    r, g, b = image.split()
    r = r.point(lambda v: min(255, int(v * warm_r)))
    b = b.point(lambda v: max(0, int(v * warm_b)))
    image = Image.merge("RGB", (r, g, b))

    image = _apply_vignette(image, strength=VIGNETTE_STRENGTH)

    sharpen_percent = round(25 * sharpen)
    if sharpen_percent > 0:
        image = image.filter(ImageFilter.UnsharpMask(radius=2, percent=sharpen_percent, threshold=3))

    if GRAIN_STRENGTH:
        image = _add_grain(image, GRAIN_STRENGTH)

    # 最後再疊 IG 的 Crema 濾鏡，等同「成品上傳 IG 時套 Crema」——自動發文的 API 不能套
    # app 內建濾鏡，所以在這裡先做好
    return apply_crema(image, CREMA_STRENGTH)


def _lift_shadows(image: Image.Image, lift: int, compress: float) -> Image.Image:
    """把黑點往上提一點（0 -> lift），同時用 compress 輕微壓縮避免整體過曝，
    讓暗部保留細節、不會死黑，是自然系食物攝影常見的「fade」處理。"""
    curve = [min(255, round(v * compress + lift)) for v in range(256)]
    return image.point(curve * 3)


def _add_grain(image: Image.Image, strength: int) -> Image.Image:
    array = np.asarray(image).astype("int16")
    noise = np.random.default_rng(0).normal(0, strength, array.shape[:2])[:, :, None]
    noisy = np.clip(array + noise, 0, 255).astype("uint8")
    return Image.fromarray(noisy, mode="RGB")


def _apply_vignette(image: Image.Image, strength: float) -> Image.Image:
    width, height = image.size
    cx, cy = width / 2, height / 2
    max_dist = math.hypot(cx, cy)

    xs, ys = np.meshgrid(np.arange(width), np.arange(height))
    dist = np.hypot(xs - cx, ys - cy) / max_dist
    darken = np.clip((dist - 0.55) / 0.45, 0.0, 1.0)  # 只影響邊角，中心不變暗
    mask = (darken * strength * 255).astype("uint8")

    black = Image.new("RGB", image.size, (0, 0, 0))
    return Image.composite(black, image, Image.fromarray(mask, mode="L"))


# Instagram Crema 濾鏡的近似參數。不是目測猜的：是拿同一張照片（宮保雞丁）在 IG 網頁版
# 套 Crema 100% 的截圖，跟原圖逐像素對齊後擬合出來的（平均誤差約 4/255）。模型刻意只用
# 「降飽和 + 每個色版一條色調曲線」，不用自由的 3x3 色彩矩陣——那張照片幾乎只有褐色系，
# 自由矩陣會擬合出極端的交叉係數，套到有綠色蔬菜、藍色盤子的照片就會偏色。
CREMA_STRENGTH = 1.0  # 使用者選定 100%（2026-10-16），所有照片一律套用
CREMA_SATURATION = 0.76
CREMA_CURVES = (  # R、G、B 三條曲線，各 17 個控制點，對應輸入 0, 1/16, ..., 1
    (0.026, 0.076, 0.125, 0.181, 0.250, 0.326, 0.406, 0.486, 0.560, 0.627, 0.691, 0.762, 0.821, 0.860, 0.895, 0.920, 0.948),
    (0.025, 0.074, 0.125, 0.186, 0.253, 0.321, 0.386, 0.448, 0.511, 0.576, 0.643, 0.719, 0.795, 0.834, 0.881, 0.919, 0.957),
    (0.055, 0.114, 0.173, 0.225, 0.262, 0.297, 0.335, 0.390, 0.459, 0.533, 0.605, 0.672, 0.734, 0.780, 0.824, 0.860, 0.882),
)


def apply_crema(image: Image.Image, strength: float = 1.0) -> Image.Image:
    """模擬 Instagram 的 Crema 濾鏡（IG 的發文 API 不能套 app 內建濾鏡，只能自己做）。
    strength 對應 IG app 裡的濾鏡強度滑桿，0~1。"""
    if strength <= 0:
        return image
    src = np.asarray(image).astype("float32") / 255.0
    lum = (src @ np.array([0.299, 0.587, 0.114], dtype="float32"))[..., None]
    desat = lum + (src - lum) * CREMA_SATURATION

    xs = np.linspace(0, 1, len(CREMA_CURVES[0]))
    out = np.stack([np.interp(desat[..., c], xs, CREMA_CURVES[c]) for c in range(3)], axis=-1)

    blended = src + (out - src) * strength
    return Image.fromarray((np.clip(blended, 0, 1) * 255 + 0.5).astype("uint8"), mode="RGB")


def _resize_to_output(image: Image.Image) -> Image.Image:
    width, height = image.size
    if width >= height:
        new_w = OUTPUT_LONG_SIDE
        new_h = round(OUTPUT_LONG_SIDE * RATIO_SHORT / RATIO_LONG)
    else:
        new_h = OUTPUT_LONG_SIDE
        new_w = round(OUTPUT_LONG_SIDE * RATIO_SHORT / RATIO_LONG)
    return image.resize((new_w, new_h), Image.LANCZOS)


def process(
    input_path: Path,
    offset_x: float,
    offset_y: float,
    zoom: float,
    orientation: str = "landscape",
    rotate: float = 0,
    warmth: float = 1.0,
    sharpen: float = 1.0,
    brightness: float = 1.0,
) -> Image.Image:
    image = _load_image(input_path)
    if abs(rotate) > 0.01:
        image = image.rotate(rotate, expand=True)  # 正值 = 逆時針；可以是任意角度，不限 90 的倍數
    if orientation == "auto":
        orientation = "landscape" if image.width >= image.height else "portrait"
    box = compute_crop_box(image.width, image.height, offset_x, offset_y, zoom, orientation)
    cropped = image.crop(box)
    styled = apply_style(cropped, warmth, sharpen, brightness)
    return _resize_to_output(styled)


def cmd_list_new(_args: argparse.Namespace) -> None:
    from cloud_import import import_new_photos

    for p in import_new_photos():  # 先把手機傳到 Google Drive 的新照片拉進 input/
        print(f"從雲端資料夾匯入：{p.name}", file=sys.stderr)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    processed_stems = set(manifest.keys())
    new_files = [
        str(p)
        for p in sorted(INPUT_DIR.glob("*"))
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and p.stem not in processed_stems
    ]
    print(json.dumps(new_files, ensure_ascii=False, indent=2))


def cmd_auto_detect(args: argparse.Namespace) -> None:
    input_path = Path(args.input_path)
    params = auto_detect_params(input_path)
    if params is None:
        print(json.dumps({"detected": False, "reason": "偵測不到明顯主體，請用手動參數"}, ensure_ascii=False))
    else:
        print(json.dumps({"detected": True, **params}, ensure_ascii=False, indent=2))


def _process_from_args(args: argparse.Namespace) -> Image.Image:
    return process(
        Path(args.input_path),
        args.offset_x,
        args.offset_y,
        args.zoom,
        args.orientation,
        args.rotate,
        args.warmth,
        args.sharpen,
        args.brightness,
    )


def cmd_preview(args: argparse.Namespace) -> None:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    input_path = Path(args.input_path)
    result = _process_from_args(args)
    preview_path = PREVIEW_DIR / f"{input_path.stem}.jpg"
    result.save(preview_path, quality=90)
    print(json.dumps({"preview_path": str(preview_path)}, ensure_ascii=False, indent=2))


def cmd_finalize(args: argparse.Namespace) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    input_path = Path(args.input_path)
    result = _process_from_args(args)

    stem = input_path.stem
    if args.output:
        output_path = Path(args.output)
    else:
        label = _sanitize_filename(args.name) if args.name else stem
        output_path = OUTPUT_DIR / f"{label}.jpg"
        manifest = _load_manifest()
        old_label = manifest.get(stem)
        if old_label and old_label != label:
            old_path = OUTPUT_DIR / f"{old_label}.jpg"
            if old_path.exists():
                old_path.unlink()  # 改名/重做時清掉舊檔名的輸出，避免留下重複檔案
        manifest[stem] = label
        _save_manifest(manifest)

    result.save(output_path, quality=92)
    print(json.dumps({"output_path": str(output_path)}, ensure_ascii=False, indent=2))


def cmd_discard(args: argparse.Namespace) -> None:
    """使用者說「不用這張」時的完整清除：input/ 的照片、預覽圖、Google Drive「IG照片」裡的
    原檔（使用者要求一起刪；Drive 桌面版刪除會進雲端垃圾桶，30 天內可救回），以及已定案
    但還沒發的成品與佇列紀錄。已經發到 IG 的不處理，IG 上的貼文要使用者自己在 app 刪。"""
    from cloud_import import import_dir

    publish_state_path = OUTPUT_DIR / "publish_state.json"
    target = Path(args.input_name)
    matches = [p for p in INPUT_DIR.iterdir() if p.is_file() and target.name in (p.name, p.stem)]
    stem = matches[0].stem if matches else target.stem
    manifest = _load_manifest()
    label = manifest.get(stem)
    state = json.loads(publish_state_path.read_text(encoding="utf-8")) if publish_state_path.exists() else {}
    if label and label in state.get("posted", {}):
        raise SystemExit(f"「{label}」已經發到 IG 了：這裡不刪成品紀錄，IG 上的貼文請在 app 裡刪除。")

    removed = []

    def _remove(path: Path) -> None:
        if path.exists():
            path.unlink()
            removed.append(str(path))

    for p in matches:
        _remove(p)
    for p in list(PREVIEW_DIR.glob("*")) if PREVIEW_DIR.exists() else []:
        if p.stem in (stem, f"_sheet_{stem}", f"_compare_{stem}"):
            _remove(p)
    cloud = import_dir()
    if cloud and cloud.is_dir():
        for p in cloud.iterdir():
            if p.is_file() and p.stem == stem:
                _remove(p)
    if label:  # 已定案、還沒發：從佇列拿掉
        _remove(OUTPUT_DIR / f"{label}.jpg")
        del manifest[stem]
        _save_manifest(manifest)
        if label in state.get("priority", []):
            state["priority"].remove(label)
            publish_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        removed.append(f"manifest 紀錄：{stem} -> {label}")
    if not removed:
        raise SystemExit(f"找不到跟「{args.input_name}」有關的檔案。")
    print(json.dumps({"removed": removed, "was_queued": bool(label)}, ensure_ascii=False, indent=2))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="IG 食譜照片批次裁切與濾鏡工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-new", help="列出還沒處理過的新照片").set_defaults(func=cmd_list_new)

    discard_sub = subparsers.add_parser("discard", help="使用者不要這張照片：連同雲端原檔、預覽、未發的成品一起刪")
    discard_sub.add_argument("input_name", help="input/ 裡的檔名或不含副檔名的名稱")
    discard_sub.set_defaults(func=cmd_discard)

    auto_sub = subparsers.add_parser(
        "auto-detect", help="自動偵測一張照片的主體，算出建議的 offset-x/offset-y/zoom/rotate"
    )
    auto_sub.add_argument("input_path", help="原始照片路徑")
    auto_sub.set_defaults(func=cmd_auto_detect)

    for name, func in (("preview", cmd_preview), ("finalize", cmd_finalize)):
        sub = subparsers.add_parser(name)
        sub.add_argument("input_path", help="原始照片路徑")
        sub.add_argument(
            "--offset-x",
            type=float,
            default=0.0,
            help="水平位移比例（裁切框往右移的比例）。注意：這是移動裁切框，所以正值會讓畫面主體看起來往左偏，負值主體往右偏",
        )
        sub.add_argument(
            "--offset-y",
            type=float,
            default=0.0,
            help="垂直位移比例（裁切框往下移的比例）。注意：這是移動裁切框，所以正值會讓畫面主體看起來往上偏，負值主體往下偏",
        )
        sub.add_argument(
            "--zoom",
            type=float,
            default=1.0,
            help="預設 1.0 代表用原圖能裁到的最大 4:3 範圍；>1 時裁切框縮小、主體放大",
        )
        sub.add_argument(
            "--orientation",
            choices=["auto", "landscape", "portrait"],
            default="landscape",
            help="輸出方向，預設 landscape（統一橫向 4:3）；auto 會依原圖寬高判斷、portrait 則輸出直幅 3:4",
        )
        sub.add_argument(
            "--rotate",
            type=float,
            default=0.0,
            help="裁切前先把原圖旋轉幾度（正值=逆時針），可以是任意角度，不限 90 的倍數。90/270 適合直幅轉橫幅；小角度（例如 3.5）適合校正鏡頭沒拿水平的微幅傾斜，auto-detect 會自動算出建議值",
        )
        sub.add_argument(
            "--warmth",
            type=float,
            default=1.0,
            help="單張照片的暖色調強度倍率，預設 1.0（跟全域設定一樣）。例外用法：只有使用者針對某張照片明確要求更/更不暖時才調整，不要用來取代全域濾鏡設定",
        )
        sub.add_argument(
            "--sharpen",
            type=float,
            default=1.0,
            help="單張照片的銳化強度倍率，預設 1.0；近距離特寫容易銳化過頭有雜訊感時可以調低（例如 0.3），0 代表完全不銳化。同樣是單張例外用法，不是全域設定",
        )
        sub.add_argument(
            "--brightness",
            type=float,
            default=1.0,
            help="單張照片的亮度倍率，預設 1.0；覺得這張過曝太亮可以調低（例如 0.85）。同樣是單張例外用法，不是全域設定",
        )
        if name == "finalize":
            sub.add_argument(
                "--name",
                default=None,
                help="這道菜的名稱，finalize 會存成 output/<name>.jpg（而不是用 input 的原始亂碼檔名），並記錄進 output/manifest.json",
            )
            sub.add_argument("--output", default=None, help="自訂輸出路徑，優先於 --name（預設 output/<name 或原檔名>.jpg）")
        sub.set_defaults(func=func)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
