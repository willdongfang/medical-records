import os
import uuid
import base64
import json
import logging
import threading
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, DateTime, LargeBinary, Text, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from PIL import Image

logger = logging.getLogger(__name__)

# --- Database setup ---
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./records.db")
# Railway PostgreSQL uses postgres:// but SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class AppConfig(Base):
    __tablename__ = "app_config"

    key = Column(String(100), primary_key=True)
    value = Column(Text, default="")


class MedicalRecord(Base):
    __tablename__ = "medical_records"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False, default="other")
    note = Column(Text, default="")
    image_data = Column(LargeBinary, nullable=False)
    image_mime = Column(String(50), nullable=False)
    thumb_data = Column(LargeBinary, nullable=True)
    ocr_text = Column(Text, default="")
    ocr_tables = Column(Text, default="")  # JSON string of table data
    ocr_status = Column(String(20), default="pending")  # pending, processing, done, failed, disabled
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(bind=engine)

# Simple migration: add new columns if they don't exist (for existing deployments)
def _migrate_db():
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if insp.has_table("medical_records"):
        existing = {col["name"] for col in insp.get_columns("medical_records")}
        with engine.begin() as conn:
            if "ocr_text" not in existing:
                conn.execute(text("ALTER TABLE medical_records ADD COLUMN ocr_text TEXT DEFAULT ''"))
            if "ocr_tables" not in existing:
                conn.execute(text("ALTER TABLE medical_records ADD COLUMN ocr_tables TEXT DEFAULT ''"))
            if "ocr_status" not in existing:
                conn.execute(text("ALTER TABLE medical_records ADD COLUMN ocr_status VARCHAR(20) DEFAULT 'disabled'"))

try:
    _migrate_db()
except Exception as e:
    logger.warning(f"Migration skipped: {e}")

# --- App ---
app = FastAPI(title="Medical Records")
app.mount("/static", StaticFiles(directory="static"), name="static")

CATEGORIES = {
    "blood": "血液检查",
    "urine": "尿液检查",
    "xray": "X光/CT/MRI",
    "ultrasound": "B超",
    "ecg": "心电图",
    "prescription": "处方/药单",
    "report": "诊断报告",
    "other": "其他",
}

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB


def make_thumbnail(image_bytes: bytes, max_size: int = 400) -> bytes:
    """Create a JPEG thumbnail from image bytes."""
    img = Image.open(BytesIO(image_bytes))
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


# --- Baidu OCR Service ---
_baidu_token_cache = {"token": "", "expires": 0}


def _get_config_value(key: str) -> str:
    """Get a config value from the database."""
    db = SessionLocal()
    try:
        row = db.query(AppConfig).filter(AppConfig.key == key).first()
        return row.value if row else ""
    except Exception:
        return ""
    finally:
        db.close()


def get_ocr_keys() -> tuple:
    """Get Baidu OCR API key and secret key. Check DB first, then env vars."""
    api_key = _get_config_value("baidu_api_key")
    secret_key = _get_config_value("baidu_secret_key")
    if api_key and secret_key:
        return api_key, secret_key
    # Fallback to environment variables
    api_key = (
        os.environ.get("BAIDU_API_KEY", "")
        or os.environ.get("BAIDU_OCR_API_KEY", "")
        or os.environ.get("BAIDU_OCR_KEY", "")
    )
    secret_key = (
        os.environ.get("BAIDU_SECRET_KEY", "")
        or os.environ.get("BAIDU_OCR_SECRET_KEY", "")
        or os.environ.get("BAIDU_OCR_SECRET", "")
    )
    return api_key, secret_key


def ocr_is_enabled() -> bool:
    """Check if OCR is configured."""
    api_key, secret_key = get_ocr_keys()
    return bool(api_key and secret_key)


def get_baidu_access_token() -> str:
    """Get Baidu OCR access token with caching."""
    import time
    now = time.time()
    if _baidu_token_cache["token"] and now < _baidu_token_cache["expires"]:
        return _baidu_token_cache["token"]
    api_key, secret_key = get_ocr_keys()
    if not api_key or not secret_key:
        return ""
    try:
        resp = requests.post(
            "https://aip.baidubce.com/oauth/2.0/token",
            params={
                "grant_type": "client_credentials",
                "client_id": api_key,
                "client_secret": secret_key,
            },
            timeout=10,
        )
        data = resp.json()
        token = data.get("access_token", "")
        expires_in = data.get("expires_in", 2592000)
        _baidu_token_cache["token"] = token
        _baidu_token_cache["expires"] = now + expires_in - 600
        return token
    except Exception as e:
        logger.error(f"Failed to get Baidu access token: {e}")
        return ""


def ocr_general_text(image_bytes: bytes) -> str:
    """Call Baidu OCR general text recognition."""
    token = get_baidu_access_token()
    if not token:
        return ""
    try:
        img_b64 = base64.b64encode(image_bytes).decode()
        resp = requests.post(
            f"https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic?access_token={token}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"image": img_b64, "language_type": "CHN_ENG", "detect_direction": "true"},
            timeout=30,
        )
        data = resp.json()
        if "words_result" in data:
            lines = [item["words"] for item in data["words_result"]]
            return "\n".join(lines)
        logger.warning(f"OCR text response unexpected: {data}")
        return ""
    except Exception as e:
        logger.error(f"OCR general text failed: {e}")
        return ""


def ocr_table(image_bytes: bytes) -> list:
    """Call Baidu OCR table recognition. Returns list of tables, each table is list of rows."""
    token = get_baidu_access_token()
    if not token:
        return []
    try:
        img_b64 = base64.b64encode(image_bytes).decode()
        resp = requests.post(
            f"https://aip.baidubce.com/rest/2.0/ocr/v1/table?access_token={token}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"image": img_b64},
            timeout=30,
        )
        data = resp.json()
        if "tables_result" not in data:
            return []
        tables = []
        for table in data["tables_result"]:
            body = table.get("body", [])
            if not body:
                continue
            max_row = max(int(c.get("row_start", 0)) for c in body) + 1
            max_col = max(int(c.get("col_start", 0)) for c in body) + 1
            grid = [[""] * max_col for _ in range(max_row)]
            for cell in body:
                r = int(cell.get("row_start", 0))
                c = int(cell.get("col_start", 0))
                grid[r][c] = cell.get("words", "")
            tables.append(grid)
        return tables
    except Exception as e:
        logger.error(f"OCR table failed: {e}")
        return []


def run_ocr_background(record_id: str, image_bytes: bytes):
    """Run OCR in a background thread and update the database."""
    db = SessionLocal()
    try:
        record = db.query(MedicalRecord).filter(MedicalRecord.id == record_id).first()
        if not record:
            return
        record.ocr_status = "processing"
        db.commit()

        text = ocr_general_text(image_bytes)
        tables = ocr_table(image_bytes)

        record.ocr_text = text
        record.ocr_tables = json.dumps(tables, ensure_ascii=False) if tables else ""
        record.ocr_status = "done"
        db.commit()
    except Exception as e:
        logger.error(f"Background OCR failed for {record_id}: {e}")
        try:
            record = db.query(MedicalRecord).filter(MedicalRecord.id == record_id).first()
            if record:
                record.ocr_status = "failed"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/categories")
async def get_categories():
    return CATEGORIES


@app.post("/api/records")
async def create_record(
    title: str = Form(...),
    category: str = Form("other"),
    note: str = Form(""),
    image: UploadFile = File(...),
):
    content = await image.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(400, "Image too large (max 10MB)")
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(400, "Only image files are allowed")

    try:
        thumb = make_thumbnail(content)
    except Exception:
        thumb = None

    db = SessionLocal()
    try:
        ocr_enabled = ocr_is_enabled()
        record = MedicalRecord(
            title=title.strip(),
            category=category,
            note=note.strip(),
            image_data=content,
            image_mime=image.content_type,
            thumb_data=thumb,
            ocr_status="pending" if ocr_enabled else "disabled",
        )
        db.add(record)
        db.commit()
        record_id = record.id

        # Trigger OCR in background
        if ocr_enabled:
            t = threading.Thread(target=run_ocr_background, args=(record_id, content), daemon=True)
            t.start()

        return {"id": record_id, "message": "上传成功"}
    finally:
        db.close()


@app.get("/api/records")
async def list_records(
    category: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    db = SessionLocal()
    try:
        q = db.query(MedicalRecord).order_by(MedicalRecord.created_at.desc())
        if category:
            q = q.filter(MedicalRecord.category == category)
        total = q.count()
        records = q.offset((page - 1) * size).limit(size).all()
        items = []
        for r in records:
            items.append({
                "id": r.id,
                "title": r.title,
                "category": r.category,
                "category_label": CATEGORIES.get(r.category, r.category),
                "note": r.note,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            })
        return {"total": total, "page": page, "size": size, "items": items}
    finally:
        db.close()


@app.get("/api/records/{record_id}")
async def get_record(record_id: str):
    db = SessionLocal()
    try:
        r = db.query(MedicalRecord).filter(MedicalRecord.id == record_id).first()
        if not r:
            raise HTTPException(404, "Record not found")
        return {
            "id": r.id,
            "title": r.title,
            "category": r.category,
            "category_label": CATEGORIES.get(r.category, r.category),
            "note": r.note,
            "ocr_text": r.ocr_text or "",
            "ocr_tables": json.loads(r.ocr_tables) if r.ocr_tables else [],
            "ocr_status": r.ocr_status or "disabled",
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
    finally:
        db.close()


@app.get("/api/records/{record_id}/image")
async def get_image(record_id: str):
    db = SessionLocal()
    try:
        r = db.query(MedicalRecord).filter(MedicalRecord.id == record_id).first()
        if not r:
            raise HTTPException(404, "Record not found")
        return Response(content=r.image_data, media_type=r.image_mime)
    finally:
        db.close()


@app.get("/api/records/{record_id}/thumb")
async def get_thumb(record_id: str):
    db = SessionLocal()
    try:
        r = db.query(MedicalRecord).filter(MedicalRecord.id == record_id).first()
        if not r:
            raise HTTPException(404, "Record not found")
        if r.thumb_data:
            return Response(content=r.thumb_data, media_type="image/jpeg")
        return Response(content=r.image_data, media_type=r.image_mime)
    finally:
        db.close()


@app.delete("/api/records/{record_id}")
async def delete_record(record_id: str):
    db = SessionLocal()
    try:
        r = db.query(MedicalRecord).filter(MedicalRecord.id == record_id).first()
        if not r:
            raise HTTPException(404, "Record not found")
        db.delete(r)
        db.commit()
        return {"message": "删除成功"}
    finally:
        db.close()


@app.post("/api/records/{record_id}/reocr")
async def reocr_record(record_id: str):
    """Re-run OCR on an existing record."""
    if not ocr_is_enabled():
        raise HTTPException(400, "OCR未配置，请在设置页面配置百度OCR密钥")
    db = SessionLocal()
    try:
        r = db.query(MedicalRecord).filter(MedicalRecord.id == record_id).first()
        if not r:
            raise HTTPException(404, "Record not found")
        image_bytes = bytes(r.image_data)
        r.ocr_status = "pending"
        db.commit()
        t = threading.Thread(target=run_ocr_background, args=(record_id, image_bytes), daemon=True)
        t.start()
        return {"message": "OCR重新识别已启动"}
    finally:
        db.close()


@app.get("/api/ocr-status")
async def ocr_config_status():
    """Check if OCR is configured."""
    return {"enabled": ocr_is_enabled()}


@app.get("/api/settings/ocr")
async def get_ocr_settings():
    """Get OCR settings (keys are masked)."""
    api_key, secret_key = get_ocr_keys()
    return {
        "enabled": bool(api_key and secret_key),
        "api_key_masked": (api_key[:4] + "****" + api_key[-4:]) if api_key and len(api_key) > 8 else ("****" if api_key else ""),
        "secret_key_masked": (secret_key[:4] + "****" + secret_key[-4:]) if secret_key and len(secret_key) > 8 else ("****" if secret_key else ""),
        "source": "database" if _get_config_value("baidu_api_key") else ("env" if api_key else "none"),
    }


@app.post("/api/settings/ocr")
async def save_ocr_settings(
    api_key: str = Form(...),
    secret_key: str = Form(...),
):
    """Save OCR API keys to database."""
    if not api_key.strip() or not secret_key.strip():
        raise HTTPException(400, "API Key 和 Secret Key 不能为空")
    db = SessionLocal()
    try:
        # Upsert api_key
        row = db.query(AppConfig).filter(AppConfig.key == "baidu_api_key").first()
        if row:
            row.value = api_key.strip()
        else:
            db.add(AppConfig(key="baidu_api_key", value=api_key.strip()))
        # Upsert secret_key
        row = db.query(AppConfig).filter(AppConfig.key == "baidu_secret_key").first()
        if row:
            row.value = secret_key.strip()
        else:
            db.add(AppConfig(key="baidu_secret_key", value=secret_key.strip()))
        db.commit()
        # Clear token cache so next OCR call uses new keys
        _baidu_token_cache["token"] = ""
        _baidu_token_cache["expires"] = 0
        return {"message": "OCR配置已保存", "enabled": True}
    finally:
        db.close()


@app.post("/api/settings/ocr/test")
async def test_ocr_settings():
    """Test if OCR credentials are valid by requesting a token."""
    api_key, secret_key = get_ocr_keys()
    if not api_key or not secret_key:
        return {"success": False, "message": "未配置OCR密钥"}
    try:
        resp = requests.post(
            "https://aip.baidubce.com/oauth/2.0/token",
            params={
                "grant_type": "client_credentials",
                "client_id": api_key,
                "client_secret": secret_key,
            },
            timeout=10,
        )
        data = resp.json()
        if "access_token" in data:
            return {"success": True, "message": "OCR连接成功"}
        else:
            return {"success": False, "message": f"认证失败: {data.get('error_description', '未知错误')}"}
    except Exception as e:
        return {"success": False, "message": f"连接失败: {str(e)}"}
