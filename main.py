import os
import re
import uuid
import hmac
import hashlib
import base64
import json
import random
import logging
import threading
import urllib.parse
from datetime import datetime, timezone, timedelta
from io import BytesIO
from typing import Optional

import requests as http_requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Request, Header
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, DateTime, LargeBinary, Text, Integer, Float, Boolean, func
from sqlalchemy.orm import declarative_base, sessionmaker
from PIL import Image

logger = logging.getLogger(__name__)

# --- Database setup ---
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./records.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# --- Models ---

class AppConfig(Base):
    __tablename__ = "app_config"
    key = Column(String(100), primary_key=True)
    value = Column(Text, default="")


DEFAULT_PASSWORD_HASH = hashlib.sha256("123456".encode()).hexdigest()


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    phone = Column(String(20), unique=True, nullable=False, index=True)
    name = Column(String(50), default="")
    password_hash = Column(String(128), default=DEFAULT_PASSWORD_HASH)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime, nullable=True)


class SmsCode(Base):
    __tablename__ = "sms_codes"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    phone = Column(String(20), nullable=False, index=True)
    code = Column(String(6), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=False)
    used = Column(Integer, default=0)


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    token = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=False, index=True)
    token_type = Column(String(10), default="user")  # user or admin
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=False)


class Member(Base):
    __tablename__ = "members"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=True, index=True)
    name = Column(String(50), nullable=False)
    relation = Column(String(20), nullable=False, default="self")
    avatar_color = Column(String(20), default="#2563eb")
    is_default = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class MedicalRecord(Base):
    __tablename__ = "medical_records"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=True, index=True)
    member_id = Column(String, nullable=True)
    title = Column(String(200), nullable=False, default="")
    category = Column(String(50), nullable=False, default="other")
    note = Column(Text, default="")
    image_data = Column(LargeBinary, nullable=False)
    image_mime = Column(String(50), nullable=False)
    thumb_data = Column(LargeBinary, nullable=True)
    ocr_text = Column(Text, default="")
    ocr_tables = Column(Text, default="")
    ocr_status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class HealthIndicator(Base):
    """Structured health indicator extracted from medical records."""
    __tablename__ = "health_indicators"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=True, index=True)
    member_id = Column(String, nullable=True, index=True)
    record_id = Column(String, nullable=True, index=True)
    indicator_key = Column(String(50), nullable=False, index=True)  # e.g. "uric_acid", "fasting_glucose"
    indicator_name = Column(String(50), nullable=False)  # display name
    value = Column(Float, nullable=False)
    unit = Column(String(20), default="")
    normal_min = Column(Float, nullable=True)
    normal_max = Column(Float, nullable=True)
    is_abnormal = Column(Boolean, default=False)
    direction = Column(String(5), default="")  # "up" / "down" / ""
    visit_date = Column(DateTime, nullable=True)  # date of the visit/test
    hospital = Column(String(100), default="")
    department = Column(String(50), default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(bind=engine)


def _migrate_db():
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if insp.has_table("medical_records"):
        existing = {col["name"] for col in insp.get_columns("medical_records")}
        with engine.begin() as conn:
            for col, dtype, default in [
                ("ocr_text", "TEXT", "''"), ("ocr_tables", "TEXT", "''"),
                ("ocr_status", "VARCHAR(20)", "'disabled'"),
                ("member_id", "VARCHAR", "NULL"), ("user_id", "VARCHAR", "NULL"),
            ]:
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE medical_records ADD COLUMN {col} {dtype} DEFAULT {default}"))
    if insp.has_table("members"):
        existing = {col["name"] for col in insp.get_columns("members")}
        with engine.begin() as conn:
            if "user_id" not in existing:
                conn.execute(text("ALTER TABLE members ADD COLUMN user_id VARCHAR DEFAULT NULL"))
    if insp.has_table("users"):
        existing = {col["name"] for col in insp.get_columns("users")}
        with engine.begin() as conn:
            if "password_hash" not in existing:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN password_hash VARCHAR(128) DEFAULT '{DEFAULT_PASSWORD_HASH}'"))
    # Seed default admin password if not set
    if insp.has_table("app_config"):
        with engine.begin() as conn:
            row = conn.execute(text("SELECT value FROM app_config WHERE key='admin_password_hash'")).fetchone()
            if not row:
                default_hash = hashlib.sha256("admin123".encode()).hexdigest()
                conn.execute(text("INSERT INTO app_config (key, value) VALUES ('admin_password_hash', :v)"), {"v": default_hash})

try:
    _migrate_db()
except Exception as e:
    logger.warning(f"Migration skipped: {e}")

# --- App ---
app = FastAPI(title="小安健康档案")
app.mount("/static", StaticFiles(directory="static"), name="static")

CATEGORIES = {
    "blood": "血液检查", "urine": "尿液检查", "xray": "X光/CT/MRI",
    "ultrasound": "B超", "ecg": "心电图", "prescription": "处方/药单",
    "report": "诊断报告", "other": "其他",
}
RELATIONS = {"self": "本人", "spouse": "配偶", "parent": "父母", "child": "子女", "other": "其他"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024


def make_thumbnail(image_bytes: bytes, max_size: int = 300) -> bytes:
    img = Image.open(BytesIO(image_bytes))
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return buf.getvalue()


# --- Config helpers ---

def _get_config(key: str) -> str:
    db = SessionLocal()
    try:
        row = db.query(AppConfig).filter(AppConfig.key == key).first()
        return row.value if row else ""
    except Exception:
        return ""
    finally:
        db.close()


def _set_config(key: str, value: str):
    db = SessionLocal()
    try:
        row = db.query(AppConfig).filter(AppConfig.key == key).first()
        if row:
            row.value = value
        else:
            db.add(AppConfig(key=key, value=value))
        db.commit()
    finally:
        db.close()


# --- Auth helpers ---

def get_current_user(authorization: Optional[str] = None) -> Optional[dict]:
    """Extract user from Bearer token. Returns dict with id, phone, name or None."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    db = SessionLocal()
    try:
        at = db.query(AuthToken).filter(
            AuthToken.token == token, AuthToken.token_type == "user"
        ).first()
        if not at or at.expires_at.replace(tzinfo=None) < datetime.now(timezone.utc).replace(tzinfo=None):
            return None
        user = db.query(User).filter(User.id == at.user_id, User.is_active == 1).first()
        if not user:
            return None
        return {"id": user.id, "phone": user.phone, "name": user.name}
    finally:
        db.close()


def require_user(authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def require_admin(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "需要管理员登录")
    token = authorization[7:]
    db = SessionLocal()
    try:
        at = db.query(AuthToken).filter(
            AuthToken.token == token, AuthToken.token_type == "admin"
        ).first()
        if not at or at.expires_at.replace(tzinfo=None) < datetime.now(timezone.utc).replace(tzinfo=None):
            raise HTTPException(401, "管理员会话已过期")
        return True
    finally:
        db.close()


# --- Alibaba Cloud SMS ---

def send_aliyun_sms(phone: str, code: str) -> dict:
    """Send SMS via Alibaba Cloud API (raw HTTP, no SDK needed)."""
    access_key = _get_config("aliyun_access_key")
    access_secret = _get_config("aliyun_access_secret")
    sign_name = _get_config("aliyun_sms_sign")
    template_code = _get_config("aliyun_sms_template")
    if not all([access_key, access_secret, sign_name, template_code]):
        return {"Code": "CONFIG_ERROR", "Message": "短信服务未配置"}

    params = {
        "AccessKeyId": access_key,
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": "cn-hangzhou",
        "SignName": sign_name,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": str(uuid.uuid4()),
        "SignatureVersion": "1.0",
        "TemplateCode": template_code,
        "TemplateParam": json.dumps({"code": code}),
        "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2017-05-25",
    }
    # Build signature
    sorted_qs = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
                         for k, v in sorted(params.items()))
    string_to_sign = "GET&%2F&" + urllib.parse.quote(sorted_qs, safe='')
    h = hmac.new((access_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1)
    params["Signature"] = base64.b64encode(h.digest()).decode()

    try:
        resp = http_requests.get("https://dysmsapi.aliyuncs.com/", params=params, timeout=10)
        return resp.json()
    except Exception as e:
        logger.error(f"SMS send failed: {e}")
        return {"Code": "NETWORK_ERROR", "Message": str(e)}


# --- Baidu OCR Service ---
_baidu_token_cache = {"token": "", "expires": 0}


def get_ocr_keys() -> tuple:
    api_key = _get_config("baidu_api_key")
    secret_key = _get_config("baidu_secret_key")
    if api_key and secret_key:
        return api_key, secret_key
    api_key = os.environ.get("BAIDU_API_KEY", "") or os.environ.get("BAIDU_OCR_API_KEY", "")
    secret_key = os.environ.get("BAIDU_SECRET_KEY", "") or os.environ.get("BAIDU_OCR_SECRET_KEY", "")
    return api_key, secret_key


def ocr_is_enabled() -> bool:
    a, s = get_ocr_keys()
    return bool(a and s)


def get_baidu_access_token() -> str:
    import time
    now = time.time()
    if _baidu_token_cache["token"] and now < _baidu_token_cache["expires"]:
        return _baidu_token_cache["token"]
    api_key, secret_key = get_ocr_keys()
    if not api_key or not secret_key:
        return ""
    try:
        resp = http_requests.post(
            "https://aip.baidubce.com/oauth/2.0/token",
            params={"grant_type": "client_credentials", "client_id": api_key, "client_secret": secret_key},
            timeout=10)
        data = resp.json()
        token = data.get("access_token", "")
        _baidu_token_cache["token"] = token
        _baidu_token_cache["expires"] = now + data.get("expires_in", 2592000) - 600
        return token
    except Exception as e:
        logger.error(f"Baidu token failed: {e}")
        return ""


def ocr_general_text(image_bytes: bytes) -> str:
    token = get_baidu_access_token()
    if not token:
        return ""
    try:
        resp = http_requests.post(
            f"https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic?access_token={token}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"image": base64.b64encode(image_bytes).decode(), "language_type": "CHN_ENG", "detect_direction": "true"},
            timeout=30)
        data = resp.json()
        if "words_result" in data:
            return "\n".join(item["words"] for item in data["words_result"])
        return ""
    except Exception as e:
        logger.error(f"OCR text failed: {e}")
        return ""


def ocr_table(image_bytes: bytes) -> list:
    token = get_baidu_access_token()
    if not token:
        return []
    try:
        resp = http_requests.post(
            f"https://aip.baidubce.com/rest/2.0/ocr/v1/table?access_token={token}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"image": base64.b64encode(image_bytes).decode()},
            timeout=30)
        data = resp.json()
        if "tables_result" not in data:
            return []
        tables = []
        for table in data["tables_result"]:
            body = table.get("body", [])
            if not body:
                continue
            mr = max(int(c.get("row_start", 0)) for c in body) + 1
            mc = max(int(c.get("col_start", 0)) for c in body) + 1
            grid = [[""] * mc for _ in range(mr)]
            for cell in body:
                grid[int(cell.get("row_start", 0))][int(cell.get("col_start", 0))] = cell.get("words", "")
            tables.append(grid)
        return tables
    except Exception as e:
        logger.error(f"OCR table failed: {e}")
        return []


def run_ocr_background(record_id: str, image_bytes: bytes):
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
        if not record.title and text:
            record.title = text.split("\n")[0].strip()[:50] or record.title
        db.commit()
    except Exception as e:
        logger.error(f"OCR background failed: {e}")
        try:
            record = db.query(MedicalRecord).filter(MedicalRecord.id == record_id).first()
            if record:
                record.ocr_status = "failed"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ==================== ROUTES ====================

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("static/admin.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/categories")
async def get_categories():
    return CATEGORIES


@app.get("/api/relations")
async def get_relations():
    return RELATIONS


# --- Auth endpoints ---

@app.post("/api/auth/send-code")
async def send_code(phone: str = Form(...)):
    phone = phone.strip()
    if not re.match(r"^1[3-9]\d{9}$", phone):
        raise HTTPException(400, "手机号格式不正确")
    # Rate limit: max 1 code per 60 seconds
    db = SessionLocal()
    try:
        recent = db.query(SmsCode).filter(
            SmsCode.phone == phone,
            SmsCode.created_at > datetime.now(timezone.utc) - timedelta(seconds=60)
        ).first()
        if recent:
            raise HTTPException(429, "发送太频繁，请60秒后重试")

        code = f"{random.randint(0, 999999):06d}"
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.add(SmsCode(phone=phone, code=code, expires_at=expires))
        db.commit()

        # Send SMS
        sms_result = send_aliyun_sms(phone, code)
        if sms_result.get("Code") == "OK":
            return {"message": "验证码已发送"}
        elif sms_result.get("Code") == "CONFIG_ERROR":
            # SMS not configured - return code in dev mode (remove in production!)
            logger.warning(f"SMS not configured. Code for {phone}: {code}")
            return {"message": "验证码已发送", "_dev_code": code}
        else:
            logger.error(f"SMS failed: {sms_result}")
            raise HTTPException(500, f"短信发送失败: {sms_result.get('Message', '未知错误')}")
    finally:
        db.close()


@app.post("/api/auth/verify")
async def verify_code(phone: str = Form(...), code: str = Form(...)):
    phone = phone.strip()
    code = code.strip()
    db = SessionLocal()
    try:
        sms = db.query(SmsCode).filter(
            SmsCode.phone == phone, SmsCode.code == code, SmsCode.used == 0,
            SmsCode.expires_at > datetime.now(timezone.utc).replace(tzinfo=None)
        ).order_by(SmsCode.created_at.desc()).first()
        if not sms:
            raise HTTPException(400, "验证码错误或已过期")
        sms.used = 1

        # Find or create user
        user = db.query(User).filter(User.phone == phone).first()
        is_new = False
        if not user:
            user = User(phone=phone, name=phone[-4:] + "用户")
            db.add(user)
            db.flush()
            is_new = True
            # Create default member for new user
            db.add(Member(user_id=user.id, name="我自己", relation="self", avatar_color="#2563eb", is_default=1))

        user.last_login = datetime.now(timezone.utc)

        # Create auth token (valid 30 days)
        token = AuthToken(
            user_id=user.id, token_type="user",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30))
        db.add(token)
        db.commit()

        return {
            "token": token.token,
            "user": {"id": user.id, "phone": user.phone, "name": user.name},
            "is_new": is_new,
        }
    finally:
        db.close()


@app.post("/api/auth/login-password")
async def login_password(phone: str = Form(...), password: str = Form(...)):
    phone = phone.strip()
    if not re.match(r"^1[3-9]\d{9}$", phone):
        raise HTTPException(400, "手机号格式不正确")
    pw_hash = hashlib.sha256(password.strip().encode()).hexdigest()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.phone == phone).first()
        is_new = False
        if not user:
            # Auto-create user with this password
            user = User(phone=phone, name=phone[-4:] + "用户", password_hash=pw_hash)
            db.add(user)
            db.flush()
            is_new = True
            db.add(Member(user_id=user.id, name="我自己", relation="self", avatar_color="#2563eb", is_default=1))
        else:
            # Verify password
            stored_hash = user.password_hash or DEFAULT_PASSWORD_HASH
            if stored_hash != pw_hash:
                raise HTTPException(400, "密码错误")
            if not user.is_active:
                raise HTTPException(403, "账号已被禁用")
        user.last_login = datetime.now(timezone.utc)
        token = AuthToken(
            user_id=user.id, token_type="user",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30))
        db.add(token)
        db.commit()
        return {
            "token": token.token,
            "user": {"id": user.id, "phone": user.phone, "name": user.name},
            "is_new": is_new,
        }
    finally:
        db.close()


@app.get("/api/auth/me")
async def auth_me(authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    if not user:
        raise HTTPException(401, "未登录")
    return user


@app.post("/api/auth/logout")
async def auth_logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        db = SessionLocal()
        try:
            db.query(AuthToken).filter(AuthToken.token == token).delete()
            db.commit()
        finally:
            db.close()
    return {"message": "已退出"}


@app.put("/api/auth/profile")
async def update_profile(name: str = Form(...), authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user["id"]).first()
        if u:
            u.name = name.strip()[:50]
            db.commit()
        return {"message": "更新成功"}
    finally:
        db.close()


# --- Member endpoints (user-scoped) ---

@app.get("/api/members")
async def list_members(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    db = SessionLocal()
    try:
        ms = db.query(Member).filter(Member.user_id == user["id"]).order_by(Member.is_default.desc(), Member.created_at.asc()).all()
        items = []
        for m in ms:
            count = db.query(func.count(MedicalRecord.id)).filter(MedicalRecord.member_id == m.id).scalar()
            items.append({
                "id": m.id, "name": m.name, "relation": m.relation,
                "relation_label": RELATIONS.get(m.relation, m.relation),
                "avatar_color": m.avatar_color or "#2563eb",
                "is_default": bool(m.is_default), "record_count": count or 0,
            })
        return items
    finally:
        db.close()


@app.post("/api/members")
async def create_member(name: str = Form(...), relation: str = Form("other"),
                        avatar_color: str = Form("#2563eb"), authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    if not name.strip():
        raise HTTPException(400, "姓名不能为空")
    db = SessionLocal()
    try:
        member = Member(user_id=user["id"], name=name.strip(), relation=relation, avatar_color=avatar_color)
        db.add(member)
        db.commit()
        return {"id": member.id, "message": "添加成功"}
    finally:
        db.close()


@app.put("/api/members/{member_id}")
async def update_member(member_id: str, name: str = Form(...), relation: str = Form("other"),
                        avatar_color: str = Form("#2563eb"), authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    db = SessionLocal()
    try:
        m = db.query(Member).filter(Member.id == member_id, Member.user_id == user["id"]).first()
        if not m:
            raise HTTPException(404, "成员不存在")
        m.name = name.strip()
        m.relation = relation
        m.avatar_color = avatar_color
        db.commit()
        return {"message": "更新成功"}
    finally:
        db.close()


@app.delete("/api/members/{member_id}")
async def delete_member(member_id: str, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    db = SessionLocal()
    try:
        m = db.query(Member).filter(Member.id == member_id, Member.user_id == user["id"]).first()
        if not m:
            raise HTTPException(404, "成员不存在")
        db.query(MedicalRecord).filter(MedicalRecord.member_id == member_id).update({"member_id": None})
        db.delete(m)
        db.commit()
        return {"message": "删除成功"}
    finally:
        db.close()


# --- Record endpoints (user-scoped) ---

@app.post("/api/records")
async def create_record(image: UploadFile = File(...), title: str = Form(""),
                        category: str = Form("other"), note: str = Form(""),
                        member_id: Optional[str] = Form(None), authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    content = await image.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(400, "图片太大（最大10MB）")
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(400, "只支持图片文件")
    try:
        thumb = make_thumbnail(content)
    except Exception:
        thumb = None
    final_title = title.strip() or f"{CATEGORIES.get(category, '检查')} {datetime.now().strftime('%m月%d日')}"
    db = SessionLocal()
    try:
        oe = ocr_is_enabled()
        record = MedicalRecord(
            user_id=user["id"], member_id=member_id, title=final_title,
            category=category, note=note.strip(), image_data=content,
            image_mime=image.content_type, thumb_data=thumb,
            ocr_status="pending" if oe else "disabled")
        db.add(record)
        db.commit()
        rid = record.id
        if oe:
            threading.Thread(target=run_ocr_background, args=(rid, content), daemon=True).start()
        return {"id": rid, "message": "上传成功"}
    finally:
        db.close()


@app.get("/api/records")
async def list_records(category: Optional[str] = Query(None), member_id: Optional[str] = Query(None),
                       page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                       authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    db = SessionLocal()
    try:
        q = db.query(MedicalRecord).filter(MedicalRecord.user_id == user["id"]).order_by(MedicalRecord.created_at.desc())
        if category:
            q = q.filter(MedicalRecord.category == category)
        if member_id:
            q = q.filter(MedicalRecord.member_id == member_id)
        total = q.count()
        records = q.offset((page - 1) * size).limit(size).all()
        mid_set = {r.member_id for r in records if r.member_id}
        mnames = {}
        if mid_set:
            mnames = {m.id: m.name for m in db.query(Member).filter(Member.id.in_(mid_set)).all()}
        items = [{
            "id": r.id, "title": r.title, "category": r.category,
            "category_label": CATEGORIES.get(r.category, r.category),
            "member_id": r.member_id or "", "member_name": mnames.get(r.member_id, ""),
            "note": r.note, "ocr_status": r.ocr_status or "disabled",
            "created_at": r.created_at.isoformat() if r.created_at else "",
        } for r in records]
        return {"total": total, "page": page, "size": size, "items": items}
    finally:
        db.close()


@app.get("/api/records/{record_id}")
async def get_record(record_id: str, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    db = SessionLocal()
    try:
        r = db.query(MedicalRecord).filter(MedicalRecord.id == record_id, MedicalRecord.user_id == user["id"]).first()
        if not r:
            raise HTTPException(404, "记录不存在")
        mn = ""
        if r.member_id:
            m = db.query(Member).filter(Member.id == r.member_id).first()
            mn = m.name if m else ""
        return {
            "id": r.id, "title": r.title, "category": r.category,
            "category_label": CATEGORIES.get(r.category, r.category),
            "member_id": r.member_id or "", "member_name": mn, "note": r.note,
            "ocr_text": r.ocr_text or "", "ocr_tables": json.loads(r.ocr_tables) if r.ocr_tables else [],
            "ocr_status": r.ocr_status or "disabled",
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
    finally:
        db.close()


@app.get("/api/records/{record_id}/image")
async def get_image(record_id: str, token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    auth = authorization or (f"Bearer {token}" if token else None)
    user = require_user(auth)
    db = SessionLocal()
    try:
        r = db.query(MedicalRecord).filter(MedicalRecord.id == record_id, MedicalRecord.user_id == user["id"]).first()
        if not r:
            raise HTTPException(404, "记录不存在")
        return Response(content=r.image_data, media_type=r.image_mime)
    finally:
        db.close()


@app.get("/api/records/{record_id}/thumb")
async def get_thumb(record_id: str, token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    auth = authorization or (f"Bearer {token}" if token else None)
    user = require_user(auth)
    db = SessionLocal()
    try:
        r = db.query(MedicalRecord).filter(MedicalRecord.id == record_id, MedicalRecord.user_id == user["id"]).first()
        if not r:
            raise HTTPException(404, "记录不存在")
        if r.thumb_data:
            return Response(content=r.thumb_data, media_type="image/jpeg")
        return Response(content=r.image_data, media_type=r.image_mime)
    finally:
        db.close()


@app.delete("/api/records/{record_id}")
async def delete_record(record_id: str, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    db = SessionLocal()
    try:
        r = db.query(MedicalRecord).filter(MedicalRecord.id == record_id, MedicalRecord.user_id == user["id"]).first()
        if not r:
            raise HTTPException(404, "记录不存在")
        db.delete(r)
        db.commit()
        return {"message": "删除成功"}
    finally:
        db.close()


@app.post("/api/records/{record_id}/reocr")
async def reocr_record(record_id: str, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    if not ocr_is_enabled():
        raise HTTPException(400, "OCR未配置")
    db = SessionLocal()
    try:
        r = db.query(MedicalRecord).filter(MedicalRecord.id == record_id, MedicalRecord.user_id == user["id"]).first()
        if not r:
            raise HTTPException(404, "记录不存在")
        image_bytes = bytes(r.image_data)
        r.ocr_status = "pending"
        db.commit()
        threading.Thread(target=run_ocr_background, args=(record_id, image_bytes), daemon=True).start()
        return {"message": "OCR重新识别已启动"}
    finally:
        db.close()


@app.get("/api/ocr-status")
async def ocr_config_status():
    return {"enabled": ocr_is_enabled()}


# ==================== HEALTH INDICATORS & TIMELINE ====================

# Indicator definitions (name, unit, normal range)
INDICATOR_DEFS = {
    "uric_acid":        {"name": "尿酸",       "unit": "μmol/L", "min": 208,   "max": 428},
    "fasting_glucose":  {"name": "空腹血糖",   "unit": "mmol/L", "min": 3.9,   "max": 6.1},
    "systolic_bp":      {"name": "收缩压",     "unit": "mmHg",   "min": 90,    "max": 140},
    "diastolic_bp":     {"name": "舒张压",     "unit": "mmHg",   "min": 60,    "max": 90},
    "total_cholesterol":{"name": "总胆固醇",   "unit": "mmol/L", "min": 2.8,   "max": 5.17},
    "bmi":              {"name": "BMI",        "unit": "kg/m²",  "min": 18.5,  "max": 24},
}


@app.get("/api/health-indicators")
async def get_health_indicators(
    indicator: str = Query("uric_acid"),
    member_id: str = Query(""),
    authorization: Optional[str] = Header(None),
):
    """Get time-series data for a specific health indicator."""
    user = require_user(authorization)
    db = SessionLocal()
    try:
        q = db.query(HealthIndicator).filter(
            HealthIndicator.user_id == user["id"],
            HealthIndicator.indicator_key == indicator,
        )
        if member_id:
            q = q.filter(HealthIndicator.member_id == member_id)
        rows = q.order_by(HealthIndicator.visit_date.asc()).all()

        series = []
        for r in rows:
            series.append({
                "x": r.visit_date.isoformat() if r.visit_date else r.created_at.isoformat(),
                "y": r.value,
                "hospital": r.hospital,
                "department": r.department,
                "record_id": r.record_id,
            })

        ind_def = INDICATOR_DEFS.get(indicator, {})
        return {
            "indicator": indicator,
            "name": ind_def.get("name", indicator),
            "unit": ind_def.get("unit", ""),
            "normal_min": ind_def.get("min", 0),
            "normal_max": ind_def.get("max", 0),
            "series": series,
        }
    finally:
        db.close()


@app.get("/api/health-indicators/all")
async def get_all_indicators_summary(
    member_id: str = Query(""),
    authorization: Optional[str] = Header(None),
):
    """Get latest value + abnormal count for all indicators."""
    user = require_user(authorization)
    db = SessionLocal()
    try:
        result = {}
        for key, ind_def in INDICATOR_DEFS.items():
            q = db.query(HealthIndicator).filter(
                HealthIndicator.user_id == user["id"],
                HealthIndicator.indicator_key == key,
            )
            if member_id:
                q = q.filter(HealthIndicator.member_id == member_id)
            latest = q.order_by(HealthIndicator.visit_date.desc()).first()

            # Count abnormal readings
            q2 = db.query(func.count(HealthIndicator.id)).filter(
                HealthIndicator.user_id == user["id"],
                HealthIndicator.indicator_key == key,
                HealthIndicator.is_abnormal == True,
            )
            if member_id:
                q2 = q2.filter(HealthIndicator.member_id == member_id)
            abnormal_count = q2.scalar() or 0

            result[key] = {
                "name": ind_def["name"],
                "unit": ind_def["unit"],
                "normal_min": ind_def["min"],
                "normal_max": ind_def["max"],
                "latest_value": latest.value if latest else None,
                "latest_date": (latest.visit_date.isoformat() if latest and latest.visit_date else None),
                "is_abnormal": latest.is_abnormal if latest else False,
                "direction": latest.direction if latest else "",
                "abnormal_count": abnormal_count,
                "record_id": latest.record_id if latest else None,
            }
        return result
    finally:
        db.close()


@app.get("/api/timeline")
async def get_timeline(
    member_id: str = Query(""),
    authorization: Optional[str] = Header(None),
):
    """Get visit timeline grouped by date, with aggregated indicator data."""
    user = require_user(authorization)
    db = SessionLocal()
    try:
        # Get distinct visit events (group by visit_date + hospital)
        q = db.query(HealthIndicator).filter(
            HealthIndicator.user_id == user["id"],
        )
        if member_id:
            q = q.filter(HealthIndicator.member_id == member_id)
        rows = q.order_by(HealthIndicator.visit_date.desc()).all()

        # Group by visit_date + hospital
        visits_map = {}
        for r in rows:
            date_str = r.visit_date.strftime("%Y-%m-%d") if r.visit_date else "unknown"
            key = f"{date_str}|{r.hospital}"
            if key not in visits_map:
                visits_map[key] = {
                    "date": date_str,
                    "hospital": r.hospital,
                    "department": r.department,
                    "record_id": r.record_id,
                    "indicators": {},
                    "abnormalities": [],
                    "tags": set(),
                }
            visit = visits_map[key]
            visit["indicators"][r.indicator_key] = {
                "name": r.indicator_name,
                "value": r.value,
                "unit": r.unit,
                "is_abnormal": r.is_abnormal,
                "direction": r.direction,
                "normal_min": r.normal_min,
                "normal_max": r.normal_max,
            }
            if r.is_abnormal:
                label = f"{r.indicator_name}{'偏高' if r.direction == 'up' else '偏低'} {r.value}"
                if label not in visit["abnormalities"]:
                    visit["abnormalities"].append(label)

        # Also include records that may not have indicators (as simple timeline entries)
        rec_q = db.query(MedicalRecord).filter(
            MedicalRecord.user_id == user["id"],
        )
        if member_id:
            rec_q = rec_q.filter(MedicalRecord.member_id == member_id)
        recs = rec_q.order_by(MedicalRecord.created_at.desc()).limit(50).all()

        for rec in recs:
            date_str = rec.created_at.strftime("%Y-%m-%d")
            key = f"{date_str}|{rec.title}"
            if key not in visits_map:
                visits_map[key] = {
                    "date": date_str,
                    "hospital": rec.title or "健康记录",
                    "department": "",
                    "record_id": rec.id,
                    "indicators": {},
                    "abnormalities": [],
                    "tags": [CATEGORIES.get(rec.category, rec.category)],
                }

        # Sort by date desc
        visits = sorted(visits_map.values(), key=lambda v: v["date"], reverse=True)

        # Convert tags set to list
        for v in visits:
            if isinstance(v["tags"], set):
                v["tags"] = list(v["tags"])

        return {"visits": visits}
    finally:
        db.close()


@app.get("/api/records/{record_id}/structured")
async def get_record_structured(
    record_id: str,
    authorization: Optional[str] = Header(None),
):
    """Get structured indicator data for a specific record."""
    user = require_user(authorization)
    db = SessionLocal()
    try:
        record = db.query(MedicalRecord).filter(
            MedicalRecord.id == record_id,
            MedicalRecord.user_id == user["id"],
        ).first()
        if not record:
            raise HTTPException(404, "记录不存在")

        indicators = db.query(HealthIndicator).filter(
            HealthIndicator.record_id == record_id,
        ).order_by(HealthIndicator.indicator_name.asc()).all()

        metrics = []
        for ind in indicators:
            metrics.append({
                "key": ind.indicator_key,
                "name": ind.indicator_name,
                "value": ind.value,
                "unit": ind.unit,
                "normal_min": ind.normal_min,
                "normal_max": ind.normal_max,
                "is_abnormal": ind.is_abnormal,
                "direction": ind.direction,
            })

        # Get member name
        member_name = ""
        if record.member_id:
            member = db.query(Member).filter(Member.id == record.member_id).first()
            if member:
                member_name = member.name

        return {
            "id": record.id,
            "title": record.title,
            "category": record.category,
            "category_label": CATEGORIES.get(record.category, record.category),
            "member_id": record.member_id,
            "member_name": member_name,
            "note": record.note,
            "ocr_text": record.ocr_text,
            "ocr_tables": json.loads(record.ocr_tables) if record.ocr_tables else [],
            "ocr_status": record.ocr_status,
            "created_at": record.created_at.isoformat() if record.created_at else "",
            "metrics": metrics,
            "conclusion": "",  # Could be enhanced with AI summary
        }
    finally:
        db.close()


# --- Demo Data Seeder ---

DEMO_VISITS = [
    {"date": "2025-03-01", "hospital": "浙大二院", "dept": "内分泌科", "tags": ["生化全套", "血常规"],
     "conclusion": "最近一次检查，尿酸已降至正常范围边缘，血糖稳定。BMI达标，整体向好。",
     "indicators": {"uric_acid": 425, "fasting_glucose": 5.1, "systolic_bp": 118, "diastolic_bp": 74, "total_cholesterol": 4.5, "bmi": 23.4}},
    {"date": "2025-02-15", "hospital": "浙大二院", "dept": "内分泌科", "tags": ["血常规", "生化全套"],
     "conclusion": "患者尿酸持续偏高，建议控制高嘌呤饮食，多饮水。BMI略超标，建议增加运动量。",
     "indicators": {"uric_acid": 482, "fasting_glucose": 5.6, "systolic_bp": 128, "diastolic_bp": 82, "total_cholesterol": 4.9, "bmi": 24.3}},
    {"date": "2025-01-08", "hospital": "浙大二院", "dept": "心内科", "tags": ["心电图", "血压监测"],
     "conclusion": "血压控制良好，心电图正常。继续目前用药方案，三个月后复查。",
     "indicators": {"uric_acid": 455, "fasting_glucose": 5.2, "systolic_bp": 124, "diastolic_bp": 78, "total_cholesterol": 4.7, "bmi": 23.9}},
    {"date": "2024-11-20", "hospital": "浙江省人民医院", "dept": "体检中心", "tags": ["年度体检"],
     "conclusion": "整体健康状况良好，胆固醇偏高，建议低脂饮食。血压正常，心肺功能正常。",
     "indicators": {"uric_acid": 410, "fasting_glucose": 5.3, "systolic_bp": 122, "diastolic_bp": 78, "total_cholesterol": 5.4, "bmi": 23.8}},
    {"date": "2024-09-25", "hospital": "浙江省中医院", "dept": "中医内科", "tags": ["中医体质辨识"],
     "conclusion": "体质偏湿热，建议薏仁、赤小豆等祛湿食疗。配合穴位按摩辅助降尿酸。",
     "indicators": {"uric_acid": 460, "fasting_glucose": 5.7, "systolic_bp": 130, "diastolic_bp": 82, "total_cholesterol": 5.1, "bmi": 24.2}},
    {"date": "2024-08-10", "hospital": "浙大二院", "dept": "内分泌科", "tags": ["生化全套"],
     "conclusion": "尿酸较上次有所下降，继续保持饮食控制。血糖指标正常。",
     "indicators": {"uric_acid": 445, "fasting_glucose": 5.5, "systolic_bp": 126, "diastolic_bp": 80, "total_cholesterol": 5.0, "bmi": 24.1}},
    {"date": "2024-06-10", "hospital": "浙大二院", "dept": "内分泌科", "tags": ["生化全套", "糖化血红蛋白"],
     "conclusion": "糖化血红蛋白5.8%，控制达标。尿酸仍偏高，加用非布司他。",
     "indicators": {"uric_acid": 475, "fasting_glucose": 5.6, "systolic_bp": 126, "diastolic_bp": 80, "total_cholesterol": 4.8, "bmi": 24.0}},
    {"date": "2024-05-18", "hospital": "杭州市第一人民医院", "dept": "全科", "tags": ["血常规", "尿常规"],
     "conclusion": "血常规各项指标正常，尿常规未见异常。建议半年后复查生化全套。",
     "indicators": {"uric_acid": 438, "fasting_glucose": 5.8, "systolic_bp": 130, "diastolic_bp": 84, "total_cholesterol": 5.1, "bmi": 24.5}},
    {"date": "2024-03-20", "hospital": "杭州市第一人民医院", "dept": "营养科", "tags": ["营养评估"],
     "conclusion": "BMI略有下降，饮食结构调整效果初显。继续执行地中海饮食方案。",
     "indicators": {"uric_acid": 430, "fasting_glucose": 5.4, "systolic_bp": 120, "diastolic_bp": 76, "total_cholesterol": 4.6, "bmi": 23.6}},
    {"date": "2024-02-22", "hospital": "浙大二院", "dept": "内分泌科", "tags": ["生化全套", "甲状腺功能"],
     "conclusion": "尿酸偏高，甲状腺功能正常。需注意饮食控制，减少海鲜和动物内脏摄入。",
     "indicators": {"uric_acid": 510, "fasting_glucose": 5.4, "systolic_bp": 132, "diastolic_bp": 86, "total_cholesterol": 5.2, "bmi": 24.8}},
    {"date": "2023-12-05", "hospital": "浙江省人民医院", "dept": "体检中心", "tags": ["年度体检"],
     "conclusion": "较去年体检结果，BMI有所上升，建议减重。血脂偏高，需注意。",
     "indicators": {"uric_acid": 468, "fasting_glucose": 6.3, "systolic_bp": 142, "diastolic_bp": 92, "total_cholesterol": 5.6, "bmi": 25.2}},
    {"date": "2023-09-12", "hospital": "浙大二院", "dept": "内分泌科", "tags": ["生化全套"],
     "conclusion": "血糖偏高，处于糖尿病前期，需严格控制碳水摄入并增加运动。",
     "indicators": {"uric_acid": 495, "fasting_glucose": 6.8, "systolic_bp": 136, "diastolic_bp": 88, "total_cholesterol": 5.3, "bmi": 25.0}},
    {"date": "2023-06-20", "hospital": "杭州市第一人民医院", "dept": "全科", "tags": ["血常规", "生化全套"],
     "conclusion": "多项指标异常，建议至内分泌科进一步检查。",
     "indicators": {"uric_acid": 520, "fasting_glucose": 6.5, "systolic_bp": 138, "diastolic_bp": 90, "total_cholesterol": 5.5, "bmi": 25.3}},
    {"date": "2023-03-15", "hospital": "浙大二院", "dept": "内分泌科", "tags": ["甲状腺功能", "生化全套"],
     "conclusion": "首次就诊，发现多项代谢指标异常，制定干预方案。",
     "indicators": {"uric_acid": 535, "fasting_glucose": 7.0, "systolic_bp": 145, "diastolic_bp": 94, "total_cholesterol": 5.8, "bmi": 25.8}},
]


@app.post("/api/seed-demo-data")
async def seed_demo_data(authorization: Optional[str] = Header(None)):
    """Seed demo health indicator data for the current user."""
    user = require_user(authorization)
    db = SessionLocal()
    try:
        # Check if user already has indicators
        existing = db.query(HealthIndicator).filter(
            HealthIndicator.user_id == user["id"]
        ).count()
        if existing > 0:
            return {"message": "已有演示数据", "count": existing}

        # Get user's default member
        default_member = db.query(Member).filter(
            Member.user_id == user["id"],
            Member.is_default == 1,
        ).first()
        member_id = default_member.id if default_member else ""

        count = 0
        for visit in DEMO_VISITS:
            visit_date = datetime.fromisoformat(visit["date"])
            for key, val in visit["indicators"].items():
                ind_def = INDICATOR_DEFS.get(key, {})
                is_abnormal = val > ind_def.get("max", 999) or val < ind_def.get("min", 0)
                direction = "up" if val > ind_def.get("max", 999) else ("down" if val < ind_def.get("min", 0) else "")
                hi = HealthIndicator(
                    user_id=user["id"],
                    member_id=member_id,
                    record_id="",
                    indicator_key=key,
                    indicator_name=ind_def.get("name", key),
                    value=float(val),
                    unit=ind_def.get("unit", ""),
                    normal_min=ind_def.get("min"),
                    normal_max=ind_def.get("max"),
                    is_abnormal=is_abnormal,
                    direction=direction,
                    visit_date=visit_date,
                    hospital=visit["hospital"],
                    department=visit["dept"],
                )
                db.add(hi)
                count += 1
        db.commit()
        return {"message": "演示数据创建成功", "count": count}
    finally:
        db.close()


# ==================== ADMIN ENDPOINTS ====================

@app.post("/api/admin/login")
async def admin_login(password: str = Form(...)):
    stored_hash = _get_config("admin_password_hash")
    if not stored_hash:
        stored_hash = hashlib.sha256("admin123".encode()).hexdigest()
    if hashlib.sha256(password.strip().encode()).hexdigest() != stored_hash:
        raise HTTPException(401, "密码错误")
    db = SessionLocal()
    try:
        token = AuthToken(user_id="admin", token_type="admin",
                          expires_at=datetime.now(timezone.utc) + timedelta(hours=8))
        db.add(token)
        db.commit()
        return {"token": token.token}
    finally:
        db.close()


@app.post("/api/admin/change-password")
async def admin_change_password(old_password: str = Form(...), new_password: str = Form(...),
                                authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    stored_hash = _get_config("admin_password_hash")
    if hashlib.sha256(old_password.strip().encode()).hexdigest() != stored_hash:
        raise HTTPException(400, "旧密码错误")
    if len(new_password.strip()) < 6:
        raise HTTPException(400, "新密码至少6位")
    _set_config("admin_password_hash", hashlib.sha256(new_password.strip().encode()).hexdigest())
    return {"message": "密码已更新"}


@app.get("/api/admin/dashboard")
async def admin_dashboard(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    db = SessionLocal()
    try:
        user_count = db.query(func.count(User.id)).scalar()
        record_count = db.query(func.count(MedicalRecord.id)).scalar()
        ocr_done = db.query(func.count(MedicalRecord.id)).filter(MedicalRecord.ocr_status == "done").scalar()
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today_records = db.query(func.count(MedicalRecord.id)).filter(MedicalRecord.created_at >= today_start).scalar()
        today_users = db.query(func.count(User.id)).filter(User.created_at >= today_start).scalar()
        return {
            "user_count": user_count, "record_count": record_count,
            "ocr_done_count": ocr_done, "today_records": today_records,
            "today_new_users": today_users,
            "ocr_enabled": ocr_is_enabled(),
            "sms_configured": bool(_get_config("aliyun_access_key") and _get_config("aliyun_sms_template")),
        }
    finally:
        db.close()


@app.get("/api/admin/users")
async def admin_list_users(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                           authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    db = SessionLocal()
    try:
        total = db.query(func.count(User.id)).scalar()
        users = db.query(User).order_by(User.created_at.desc()).offset((page - 1) * size).limit(size).all()
        items = []
        for u in users:
            rc = db.query(func.count(MedicalRecord.id)).filter(MedicalRecord.user_id == u.id).scalar()
            items.append({
                "id": u.id, "phone": u.phone, "name": u.name,
                "is_active": bool(u.is_active), "record_count": rc,
                "created_at": u.created_at.isoformat() if u.created_at else "",
                "last_login": u.last_login.isoformat() if u.last_login else "",
            })
        return {"total": total, "page": page, "size": size, "items": items}
    finally:
        db.close()


@app.post("/api/admin/users/{user_id}/toggle")
async def admin_toggle_user(user_id: str, authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            raise HTTPException(404, "用户不存在")
        u.is_active = 0 if u.is_active else 1
        db.commit()
        return {"message": "已禁用" if not u.is_active else "已启用", "is_active": bool(u.is_active)}
    finally:
        db.close()


@app.get("/api/admin/config/{group}")
async def admin_get_config(group: str, authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    def mask(v):
        if not v:
            return ""
        if len(v) > 8:
            return v[:4] + "****" + v[-4:]
        return "****"

    if group == "ocr":
        ak, sk = get_ocr_keys()
        return {"baidu_api_key": mask(ak), "baidu_secret_key": mask(sk), "enabled": bool(ak and sk)}
    elif group == "sms":
        return {
            "aliyun_access_key": mask(_get_config("aliyun_access_key")),
            "aliyun_access_secret": mask(_get_config("aliyun_access_secret")),
            "aliyun_sms_sign": _get_config("aliyun_sms_sign"),
            "aliyun_sms_template": _get_config("aliyun_sms_template"),
            "configured": bool(_get_config("aliyun_access_key") and _get_config("aliyun_sms_template")),
        }
    raise HTTPException(404, "未知配置组")


@app.post("/api/admin/config/{group}")
async def admin_save_config(group: str, request: Request, authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    form = await request.form()
    if group == "ocr":
        for k in ["baidu_api_key", "baidu_secret_key"]:
            v = form.get(k, "").strip()
            if v:
                _set_config(k, v)
        _baidu_token_cache["token"] = ""
        _baidu_token_cache["expires"] = 0
        return {"message": "OCR配置已保存"}
    elif group == "sms":
        for k in ["aliyun_access_key", "aliyun_access_secret", "aliyun_sms_sign", "aliyun_sms_template"]:
            v = form.get(k, "").strip()
            if v:
                _set_config(k, v)
        return {"message": "短信配置已保存"}
    raise HTTPException(404, "未知配置组")
