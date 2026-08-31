import os
import re
import glob
import json
import logging
import asyncio
import hashlib
import tempfile
import urllib.request
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Form, HTTPException, UploadFile, File, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address
import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1, TALB, APIC, USLT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_MUSICAS = os.path.join(BASE_DIR, "Musicas")
ARQ_HISTORICO = os.path.join(BASE_DIR, "historico.json")
os.makedirs(PASTA_MUSICAS, exist_ok=True)

app = FastAPI()
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

progresso_downloads = {}

def limpar_titulo(texto):
    if not texto: return ""
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", texto)
    t = re.sub(r"[^a-zA-Z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def buscar_letra(titulo, artista):
    try:
        q = urllib.parse.quote(f"{artista} {titulo}".strip())
        req = urllib.request.Request(f"https://lrclib.net/api/search?q={q}", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.load(r)
            if data and len(data) > 0:
                return data[0].get("plainLyrics") or data[0].get("syncedLyrics")
    except Exception as e:
        logger.error(f"Erro letra: {e}")
    return None

def salvar_historico(titulo, artista, url):
    hist = []
    if os.path.exists(ARQ_HISTORICO):
        try:
            with open(ARQ_HISTORICO, "r", encoding="utf-8") as f: hist = json.load(f)
        except: pass
    hist.append({"titulo": titulo, "artista": artista, "url": url, "data": datetime.now().strftime("%d/%m/%Y %H:%M")})
    with open(ARQ_HISTORICO, "w", encoding="utf-8") as f:
        json.dump(hist[-50:], f, ensure_ascii=False, indent=2)

def download_sync(url, opts, dl_id, baixados):
    def hook(d):
        if d.get("status") == "downloading":
            try:
                p = int(float(d.get("_percent_str", "0").replace("%", "").strip()))
                progresso_downloads[dl_id] = {"pct": int(p*0.8), "status": f"Baixando: {p}%"}
            except: pass
        elif d.get("status") == "finished":
            progresso_downloads[dl_id] = {"pct": 85, "status": "Convertendo MP3..."}
            path = d.get("filepath")
            if path: baixados.append(os.path.splitext(path)[0] + ".mp3")
    
    opts["progress_hooks"] = [hook]
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

@app.post("/api/upload-cookies")
async def upload_cookies(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    user_hash = hashlib.md5(request.client.host.encode()).hexdigest()[:8]
    path = os.path.join(tempfile.gettempdir(), f"cookies_{user_hash}.txt")
    with open(path, "wb") as f: f.write(content)
    logger.info(f"Cookie salvo: {path}")
    return {"path": path}

@app.get("/api/progresso/{dl_id}")
async def get_progresso(dl_id: str):
    return progresso_downloads.get(dl_id, {"pct": 0, "status": "Iniciando..."})

@app.post("/api/buscar")
async def buscar(request: Request, query: str = Form(...), cookie_path: str = Form(None)):
    if not query.startswith("http"):
        raise HTTPException(400, "Apenas links diretos são suportados nesta versão.")
    
    opts = {"quiet": True, "no_warnings": True}
    if cookie_path and os.path.exists(cookie_path):
        opts["cookiefile"] = cookie_path
        
    try:
        info = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(opts).extract_info(query, download=False))
        return {"resultados": [{"url": query, "titulo": info.get("title"), "artista": info.get("uploader"), "thumb": info.get("thumbnail"), "duracao": info.get("duration_string")}]}
    except Exception as e:
        raise HTTPException(400, detail=str(e))

@app.post("/api/download")
async def baixar(request: Request, url: str = Form(...), dl_id: str = Form(...), cookie_path: str = Form(None), capa_url: str = Form(None)):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(PASTA_MUSICAS, "%(title)s.%(ext)s"),
        "postprocessors": [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}],
        "quiet": True
    }
    if cookie_path and os.path.exists(cookie_path):
        opts["cookiefile"] = cookie_path
        
    baixados = []
    progresso_downloads[dl_id] = {"pct": 10, "status": "Conectando..."}
    
    try:
        info = await asyncio.to_thread(download_sync, url, opts, dl_id, baixados)
        titulo = info.get("title", "audio")
        artista = info.get("uploader", "")
        
        mp3_file = baixados[0] if baixados else None
        if not mp3_file or not os.path.exists(mp3_file):
            raise Exception("Falha ao gerar MP3")
            
        progresso_downloads[dl_id] = {"pct": 90, "status": "Embutindo Capa e Letra..."}
        
        # Tags basicas
        try:
            audio = MP3(mp3_file)
            if not audio.tags: audio.add_tags()
            audio["TIT2"] = TIT2(encoding=3, text=titulo)
            audio["TPE1"] = TPE1(encoding=3, text=artista)
            
            # Capa
            thumb = capa_url or info.get("thumbnail")
            if thumb:
                req = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as img:
                    audio.tags.add(APIC(encoding=3, mime=img.headers.get_content_type(), type=3, desc=u'Cover', data=img.read()))
                    
            # Letra
            letra = buscar_letra(titulo, artista)
            if letra:
                audio.tags.add(USLT(encoding=3, lang=u'eng', desc=u'Lyrics', text=letra))
                
            audio.save()
        except Exception as e:
            logger.error(f"Erro tags: {e}")
            
        salvar_historico(titulo, artista, url)
        progresso_downloads[dl_id] = {"pct": 100, "status": "Pronto!"}
        
        safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{artista} - {titulo}.mp3")
        return FileResponse(mp3_file, filename=safe_name, media_type="audio/mpeg")
        
    except Exception as e:
        progresso_downloads[dl_id] = {"pct": 0, "status": f"Erro: {str(e)}"}
        raise HTTPException(500, detail=str(e))

@app.get("/api/historico")
async def historico():
    if os.path.exists(ARQ_HISTORICO):
        with open(ARQ_HISTORICO, "r", encoding="utf-8") as f: return json.load(f)
    return []
