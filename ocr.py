import os, cv2, numpy as np
from dotenv import load_dotenv

load_dotenv()

TEMPL_DIR = os.getenv("TEMPL_DIR", "storage/templates")
os.makedirs(TEMPL_DIR, exist_ok=True)

# ---------- Bild-Helfer ----------

def _preprocess(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # leichte Kontrasterhöhung
    gray = cv2.convertScaleAbs(gray, alpha=1.2, beta=5)
    # binär (Otsu)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    return bw

def _find_board_roi(img):
    # adap. Threshold für Konturensuche
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    thr  = cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV,31,10)
    cnts, _ = cv2.findContours(thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, area_best = None, 0
    for c in cnts:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02*peri, True)
        area = cv2.contourArea(approx)
        if len(approx)==4 and area>area_best:
            best, area_best = approx, area
    if best is None:
        return img
    pts = best.reshape(4,2).astype(np.float32)
    s = pts.sum(axis=1); d = np.diff(pts, axis=1).reshape(-1)
    rect = np.zeros((4,2), dtype=np.float32)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    rect[1] = pts[np.argmin(d)]
    rect[3] = pts[np.argmax(d)]
    side = int(max(
        np.linalg.norm(rect[0]-rect[1]),
        np.linalg.norm(rect[1]-rect[2]),
        np.linalg.norm(rect[2]-rect[3]),
        np.linalg.norm(rect[3]-rect[0]),
    ))
    dst = np.array([[0,0],[side-1,0],[side-1,side-1],[0,side-1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(rect, dst)
    return cv2.warpPerspective(img, M, (side, side))

def _extract_cells(board_img, grid=5):
    h,w = board_img.shape[:2]
    pad = int(0.02*min(h,w))
    crop = board_img[pad:h-pad, pad:w-pad]
    h,w = crop.shape[:2]
    ch, cw = h//grid, w//grid
    cells = []
    for r in range(grid):
        row = []
        for c in range(grid):
            y1,y2 = r*ch, (r+1)*ch
            x1,x2 = c*cw, (c+1)*cw
            row.append(crop[y1:y2, x1:x2])
        cells.append(row)
    return cells

# ---------- Template-Store ----------

def _templ_path(digit:int):
    return os.path.join(TEMPL_DIR, f"d{digit}.png")

def templates_available():
    return all(os.path.exists(_templ_path(d)) for d in range(10))

def _save_templates(means):  # means: dict[digit] -> list[np.array]
    for d in range(10):
        if len(means.get(d, []))==0: 
            continue
        m = np.mean(np.stack(means[d], axis=0), axis=0).astype(np.uint8)
        cv2.imwrite(_templ_path(d), m)

def _load_templates():
    templ = {}
    for d in range(10):
        p = _templ_path(d)
        if os.path.exists(p):
            im = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
            templ[d] = im
    return templ

# ---------- Ziffern-Segmentierung & Matching ----------

def _segment_digits(cell_bgr):
    """Gibt eine Liste binärer, normalisierter ROIs (28x28) für 1-2 Ziffern zurück."""
    bw = _preprocess(cell_bgr)
    h,w = bw.shape
    # mittleres Rechteck auswerten (Rand raus)
    y1,y2 = h//10, h - h//10
    x1,x2 = w//10, w - w//10
    roi = bw[y1:y2, x1:x2]

    # kleine Punkte entfernen
    roi = cv2.morphologyEx(roi, cv2.MORPH_OPEN, np.ones((3,3),np.uint8))

    # Komponenten
    cnts,_ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in cnts:
        x,y,wc,hc = cv2.boundingRect(c)
        if wc*hc < 0.01*roi.size: 
            continue
        boxes.append((x,y,wc,hc))
    if not boxes:
        return []

    boxes.sort(key=lambda b:b[0])  # links->rechts

    rois = []
    for (x,y,wc,hc) in boxes:
        digit = roi[y:y+hc, x:x+wc]
        # auf 28x28 normieren mit Padding
        scale = 22 / max(wc, hc)
        resized = cv2.resize(digit, (int(wc*scale), int(hc*scale)), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((28,28), dtype=np.uint8)
        ys = (28 - resized.shape[0])//2
        xs = (28 - resized.shape[1])//2
        canvas[ys:ys+resized.shape[0], xs:xs+resized.shape[1]] = resized
        rois.append(canvas)
    # max 2 Ziffern
    if len(rois) > 2:
        areas = [cv2.countNonZero(r) for r in rois]
        idx = np.argsort(areas)[-2:]
        rois = [rois[i] for i in sorted(idx)]
    return rois

def _ncc(a,b):
    a = a.astype(np.float32); b = b.astype(np.float32)
    a = (a - a.mean()) / (a.std()+1e-6)
    b = (b - b.mean()) / (b.std()+1e-6)
    return float((a*b).mean())

def _match_digit(roi, templs):
    best, best_d = -1.0, None
    for d, t in templs.items():
        if t.shape != roi.shape:
            t = cv2.resize(t, (roi.shape[1], roi.shape[0]), interpolation=cv2.INTER_AREA)
        s = _ncc(roi, t)
        if s > best:
            best, best_d = s, d
    return best_d, best

# ---------- Öffentliche API ----------

def train_templates_from_board(image_path:str, labels_25:list):
    """
    Trainiert Templates (0-9), indem es aus einem sauberen Board die Ziffern extrahiert.
    labels_25: 25 Strings (Zahl oder 'FREE'), zeilenweise.
    """
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError("Could not read training image.")
    board = _find_board_roi(img)
    cells = _extract_cells(board, 5)

    means = {d:[] for d in range(10)}
    idx = 0
    for r in range(5):
        for c in range(5):
            lab = str(labels_25[idx]).strip().upper()
            idx += 1
            if r==2 and c==2:
                # Roboter-Feld = FREE
                continue
            if lab == "FREE":
                continue
            rois = _segment_digits(cells[r][c])
            if len(rois)==0:
                continue
            digits = list(lab)
            if not all(ch.isdigit() for ch in digits):
                continue
            if len(digits) != len(rois):
                # Fallback: 2-stellig aber 1 ROI -> halbieren
                if len(digits)==2 and len(rois)==1:
                    h,w = rois[0].shape
                    rois = [rois[0][:, :w//2], rois[0][:, w//2:]]
                else:
                    continue
            for ch, roi in zip(digits, rois):
                d = int(ch)
                means[d].append(roi)
    _save_templates(means)
    return templates_available()

def image_to_grid(image_path:str):
    """
    Liest ein Board mit Template-Matching (wenn Templates vorhanden),
    sonst konservativ 'ERR'. Mitte (2,2) ist immer None (FREE).
    """
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError("Could not read image.")
    board = _find_board_roi(img)
    cells = _extract_cells(board, 5)

    templs = _load_templates()
    use_templates = len(templs) == 10

    grid = []
    for r in range(5):
        row = []
        for c in range(5):
            if r==2 and c==2:
                row.append(None)  # FREE (Roboter)
                continue
            rois = _segment_digits(cells[r][c])
            if len(rois)==0:
                row.append('ERR'); continue
            digits = []
            for roi in rois:
                if use_templates:
                    d, score = _match_digit(roi, templs)
                    if d is None or score < 0.60:
                        digits = ['ERR']; break
                    digits.append(str(d))
                else:
                    digits = ['ERR']; break
            if 'ERR' in digits or len(digits)==0:
                row.append('ERR')
            else:
                try:
                    row.append(int("".join(digits)))
                except:
                    row.append('ERR')
        grid.append(row)
    # Plausibilitäts-Check 1..75
    for r in range(5):
        for c in range(5):
            v = grid[r][c]
            if v is None:
                continue
            if not isinstance(v, int) or v < 1 or v > 75:
                grid[r][c] = 'ERR'
    return grid
