import os
import re
import glob
import json
import logging
import asyncio
import tempfile
import urllib.request
import urllib.parse

from fastapi import FastAPI, Form, HTTPException, UploadFile, File, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1, APIC, USLT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_MUSICAS = os.path.join(BASE_DIR, "Musicas")
os.makedirs(PASTA_MUSICAS, exist_ok=True)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

progresso_downloads = {}


def buscar_letra(titulo, artista):
    try:
        q = urllib.parse.quote(f"{artista} {titulo}".strip())
        req = urllib.request.Request(
            f"https://lrclib.net/api/search?q={q}",
            headers={"User-Agent": "SoundCloudMP3/1.0"}
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.load(r)
            if data and len(data) > 0:
                return data[0].get("plainLyrics") or data[0].get("syncedLyrics")
    except Exception as e:
        logger.warning(f"Letra nao encontrada: {e}")
    return None


def download_sync(url, opts, dl_id, baixados):
    def hook(d):
        if d.get("status") == "downloading":
            try:
                p = int(float(d.get("_percent_str", "0").replace("%", "").strip()))
                progresso_downloads[dl_id] = {"pct": 10 + int(p * 0.7), "status": f"Baixando audio: {p}%"}
            except Exception:
                pass
        elif d.get("status") == "finished":
            progresso_downloads[dl_id] = {"pct": 85, "status": "Convertendo para MP3 320kbps..."}
            caminho = d.get("filepath")
            if caminho:
                baixados.append(os.path.splitext(caminho)[0] + ".mp3")

    opts["progress_hooks"] = [hook]
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.get("/api/progresso/{dl_id}")
async def get_progresso(dl_id: str):
    return progresso_downloads.get(dl_id, {"pct": 0, "status": "Iniciando..."})


@app.post("/api/download")
async def baixar(
    request: Request,
    url: str = Form(...),
    dl_id: str = Form(...),
    capa_url: str = Form(None),
    cookie_file: UploadFile = File(None),
):
    # O cookie chega JUNTO com o download agora. Uma requisicao so. Sem estado. Sem caminho temp pra validar.
    cookie_path = None
    if cookie_file is not None:
        conteudo = await cookie_file.read()
        if conteudo:
            cookie_path = os.path.join(tempfile.gettempdir(), f"cookies_{dl_id}.txt")
            with open(cookie_path, "wb") as f:
                f.write(conteudo)
            logger.info(f"COOKIES RECEBIDOS: {len(conteudo)} bytes de {request.client.host}")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(PASTA_MUSICAS, f"{dl_id}_%(title)s.%(ext)s"),
        "noplaylist": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}
        ],
        "quiet": True,
        "no_warnings": True,
    }
    if cookie_path:
        opts["cookiefile"] = cookie_path

    baixados = []
    modo = "COM cookies" if cookie_path else "SEM cookies"
    progresso_downloads[dl_id] = {"pct": 10, "status": f"Conectando ({modo})..."}

    try:
        info = await asyncio.to_thread(download_sync, url, opts, dl_id, baixados)
        titulo = info.get("title", "audio")
        artista = info.get("uploader", "")

        mp3 = baixados[0] if baixados else None
        if not mp3 or not os.path.exists(mp3):
            g = glob.glob(os.path.join(PASTA_MUSICAS, f"{dl_id}_*.mp3"))
            if g:
                mp3 = g[0]
        if not mp3 or not os.path.exists(mp3):
            raise Exception("MP3 nao foi gerado (verifique FFmpeg no Render)")

        progresso_downloads[dl_id] = {"pct": 90, "status": "Embutindo capa e letra..."}
        try:
            audio = MP3(mp3)
            if audio.tags is None:
                audio.add_tags()
            audio["TIT2"] = TIT2(encoding=3, text=titulo)
            audio["TPE1"] = TPE1(encoding=3, text=artista)

            thumb = capa_url or info.get("thumbnail")
            if thumb:
                req = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as img:
                    audio.tags.add(APIC(encoding=3, mime=img.headers.get_content_type() or "image/jpeg", type=3, desc="Cover", data=img.read()))

            letra = buscar_letra(titulo, artista)
            if letra:
                audio.tags.add(USLT(encoding=3, lang="eng", desc="Lyrics", text=letra))
            audio.save()
        except Exception as e:
            logger.warning(f"Tags/capa/letra falharam (nao bloqueia download): {e}")

        progresso_downloads[dl_id] = {"pct": 100, "status": "Pronto!"}
        nome = re.sub(r'[\\/*?:"<>|]', "", f"{artista} - {titulo}.mp3")
        return FileResponse(mp3, filename=nome, media_type="audio/mpeg")
    except Exception as e:
        logger.error(f"ERRO no download: {e}")
        progresso_downloads[dl_id] = {"pct": 0, "status": "Erro"}
        raise HTTPException(status_code=500, detail=str(e))