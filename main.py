import os
import uuid
import base64
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, DateTime, LargeBinary, Text, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from PIL import Image

# --- Database setup ---
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./records.db")
# Railway PostgreSQL uses postgres:// but SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class MedicalRecord(Base):
    __tablename__ = "medical_records"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False, default="other")
    note = Column(Text, default="")
    image_data = Column(LargeBinary, nullable=False)
    image_mime = Column(String(50), nullable=False)
    thumb_data = Column(LargeBinary, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(bind=engine)

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
        record = MedicalRecord(
            title=title.strip(),
            category=category,
            note=note.strip(),
            image_data=content,
            image_mime=image.content_type,
            thumb_data=thumb,
        )
        db.add(record)
        db.commit()
        return {"id": record.id, "message": "上传成功"}
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
