import os
import sqlite3
import secrets
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Header, Depends
from fastapi.responses import FileResponse

UPLOAD_DIR = Path("/app/uploads")
DB_PATH = Path("/app/db/gorka.db")
ADMIN_PIN = os.getenv("ADMIN_PIN", "1234")
QUOTA_BYTES = int(float(os.getenv("STORAGE_QUOTA_GB", "15")) * (1024 ** 3))

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".bmp", ".heic"}

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Gorka-cloud API")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS images (
                id TEXT PRIMARY KEY,
                orig_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                mime_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


init_db()


def verify_admin(x_admin_pin: str = Header(default="")):
    if ADMIN_PIN and x_admin_pin != ADMIN_PIN:
        raise HTTPException(status_code=401, detail="Неверный PIN-код доступа")


@app.get("/api/images", dependencies=[Depends(verify_admin)])
def list_images():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM images ORDER BY created_at DESC").fetchall()
        images = [dict(r) for r in rows]
        used_bytes = sum(img["size"] for img in images)
    return {
        "images": images,
        "used_bytes": used_bytes,
        "quota_bytes": QUOTA_BYTES
    }


@app.post("/api/upload", dependencies=[Depends(verify_admin)])
async def upload_image(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Формат {ext} не поддерживается")

    content = await file.read()
    file_size = len(content)

    with get_db() as conn:
        used_bytes = conn.execute("SELECT COALESCE(SUM(size), 0) FROM images").fetchone()[0]
        if used_bytes + file_size > QUOTA_BYTES:
            raise HTTPException(status_code=507, detail="Превышен лимит хранилища Gorka-cloud")

        img_id = secrets.token_urlsafe(6)
        stored_name = f"{img_id}{ext}"
        file_path = UPLOAD_DIR / stored_name

        with open(file_path, "wb") as f:
            f.write(content)

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        conn.execute(
            "INSERT INTO images (id, orig_name, stored_name, size, mime_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (img_id, file.filename, stored_name, file_size, file.content_type or "image/jpeg", now_iso)
        )
        conn.commit()

    return {"id": img_id, "orig_name": file.filename, "stored_name": stored_name, "size": file_size}


@app.delete("/api/images/{img_id}", dependencies=[Depends(verify_admin)])
def delete_image(img_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT stored_name FROM images WHERE id = ?", (img_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Файл не найден")

        file_path = UPLOAD_DIR / row["stored_name"]
        if file_path.exists():
            file_path.unlink()

        conn.execute("DELETE FROM images WHERE id = ?", (img_id,))
        conn.commit()
    return {"status": "deleted"}


# Публичные эндпоинты для QR-ссылок (только просмотр и скачивание)
@app.get("/api/public/{img_id}")
def get_public_image(img_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, orig_name, stored_name, size, created_at FROM images WHERE id = ?",
            (img_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Картинка не найдена или была удалена")
        return dict(row)


@app.get("/api/download/{img_id}")
def download_public_image(img_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT orig_name, stored_name, mime_type FROM images WHERE id = ?",
            (img_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Файл не найден")

        file_path = UPLOAD_DIR / row["stored_name"]
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Файл отсутствует на диске")

        return FileResponse(
            path=file_path,
            media_type=row["mime_type"],
            filename=row["orig_name"]
        )
