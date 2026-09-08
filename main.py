import os
import re
import glob
import json
import sys
import time
import socket
import ipaddress
import subprocess
import urllib.request
import urllib.parse
import concurrent.futures
import threading
import uuid
from datetime import datetime
from collections import deque
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1, TALB, APIC, USLT

# ==============================================================================
# CONFIGURAÇÕES GERAIS
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_MUSICAS = os.path.join(BASE_DIR, "Musicas")
ARQ_HISTORICO = os.path.join(BASE_DIR, "historico.json")
QUALIDADE_MP3 = "320"
os.makedirs(PASTA_MUSICAS, exist_ok=True)

# Config via env
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
FILE_RETENTION_MIN = int(os.environ.get("FILE_RETENTION_MINUTES", "60"))
ALLOWED_DOMAINS = [
    d.strip().lower()
    for d in os.environ.get("ALLOWED_DOMAINS", "soundcloud.com,snd.sc,on.soundcloud.com").split(",")
    if d.strip()
]

# ==============================================================================
# RATE LIMITER
# ==============================================================================
limiter = Limiter(key_func=get_remote_address, default_limits=[f"{os.environ.get('RATE_LIMIT_PER_MINUTE', '15')}/minute"])

# ==============================================================================
# SSRF PROTECTION
# ==============================================================================
def validar_url_segura(url: str) -> str:
    """Valida URL contra SSRF: checa domínio allowlist e bloqueia IPs privados."""
    if not url or not url.strip():
        raise ValueError("URL vazia.")
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("URL malformada.")
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Apenas HTTP/HTTPS permitidos.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Host inválido.")
    # Allowlist de domínios
    if ALLOWED_DOMAINS:
        ok = any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)
        if not ok:
            raise ValueError(f"Domínio não permitido: {host}. Permitidos: {', '.join(ALLOWED_DOMAINS)}")
    # Resolver e checar IP privado
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError(f"Não foi possível resolver o host: {host}")
    for fam, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved or ip_obj.is_multicast:
            raise ValueError(f"Endereço IP bloqueado por segurança: {ip_str}")
    return url

# ==============================================================================
# FILA DE DOWNLOADS + CANCELAMENTO
# ==============================================================================
download_semaphore = threading.Semaphore(MAX_CONCURRENT)
active_downloads = {}  # download_id -> {"cancel": threading.Event(), "process": None, "status": str, "queue_pos": int}
queue_lock = threading.Lock()
download_queue = deque()  # ids waiting

def register_download(download_id: str):
    with queue_lock:
        active_downloads[download_id] = {
            "cancel": threading.Event(),
            "status": "Na fila...",
            "queue_pos": len(download_queue) + 1,
            "started_at": None,
        }
        download_queue.append(download_id)

def acquire_slot(download_id: str):
    """Blocks until slot available or cancelled."""
    evt = active_downloads.get(download_id, {}).get("cancel")
    # Try to acquire with polling so we can respect cancel
    while not download_semaphore.acquire(blocking=False):
        if evt and evt.is_set():
            raise RuntimeError("Download cancelado pelo usuário.")
        time.sleep(0.3)
    with queue_lock:
        if download_id in active_downloads:
            active_downloads[download_id]["status"] = "Processando..."
            active_downloads[download_id]["started_at"] = time.time()
            if download_id in download_queue:
                download_queue.remove(download_id)
            # Recalc positions
            for i, did in enumerate(download_queue):
                if did in active_downloads:
                    active_downloads[did]["queue_pos"] = i + 1

def release_slot(download_id: str):
    download_semaphore.release()
    with queue_lock:
        active_downloads.pop(download_id, None)

def is_cancelled(download_id: str) -> bool:
    info = active_downloads.get(download_id)
    return bool(info and info["cancel"].is_set())

# ==============================================================================
# CLEANUP EM BACKGROUND
# ==============================================================================
def cleanup_loop():
    """Remove arquivos antigos periodicamente, independente de tráfego."""
    while True:
        try:
            time.sleep(60)  # checa a cada minuto
            cutoff = time.time() - (FILE_RETENTION_MIN * 60)
            for fname in os.listdir(PASTA_MUSICAS):
                fpath = os.path.join(PASTA_MUSICAS, fname)
                try:
                    if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                except Exception:
                    pass
        except Exception:
            time.sleep(60)

cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
cleanup_thread.start()

# ==============================================================================
# AUTO-UPDATE DO YT-DLP (opcional)
# ==============================================================================
def atualizar_ytdlp_se_necessario():
    if os.environ.get("AUTO_UPDATE_YTDLP", "").lower() != "true":
        return
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            check=False, timeout=60, capture_output=True, text=True
        )
    except Exception:
        pass

atualizar_ytdlp_se_necessario()

# ==============================================================================
# FUNÇÕES AUXILIARES
# ==============================================================================
STOP = {"the", "and", "for", "with", "official", "video", "lyric", "lyrics",
        "slowed", "reverb", "extended", "remix", "speed", "ultra", "super",
        "com", "sem", "pra", "pro", "uma", "não", "nao", "sped", "up", "edit"}

progresso_downloads = {}

def palavras(s):
    return {w for w in re.findall(r"[a-z0-9]{2,}", (s or "").lower())} - STOP

def limpar_ansi(texto):
    return re.sub(r"\x1b\[[0-9;]*m", "", texto)

def limpar_titulo_para_busca(texto):
    if not texto:
        return ""
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", texto)
    t = re.sub(r"(?i)\b(prod\.|prod|feat\.|feat|ft\.|ft|official|audio|video|lyric|lyrics|sped up|slowed|reverb)\b", " ", t)
    t = re.sub(r"[\^_\*~•★\-\|/\\:;<=>\?@#\$%&!\+\"]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def fmt_duracao(segundos):
    if segundos is None:
        return ""
    try:
        seg = int(float(segundos))
        return f"{seg // 60}:{seg % 60:02d}"
    except Exception:
        return ""

def obter_og_image(url):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=4) as r:
            html = r.read(250000).decode("utf-8", "ignore")
        m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        if m:
            return m.group(1)
        m2 = re.search(r'<meta\s+content="([^"]+)"\s+property="og:image"', html)
        if m2:
            return m2.group(1)
        return None
    except Exception:
        return None

# ==============================================================================
# MAPEAMENTO DE ERROS HUMANOS
# ==============================================================================
def erro_humano(exc: Exception) -> str:
    msg = limpar_ansi(str(exc)).lower()
    if "private" in msg or "privado" in msg:
        return "🔒 Esta faixa é privada e não pode ser baixada."
    if "geo" in msg or "region" in msg or "country" in msg:
        return "🌍 Faixa bloqueada na sua região (geo-restriction)."
    if "404" in msg or "not found" in msg or "não encontrado" in msg:
        return "🔗 Link inválido ou faixa removida do SoundCloud."
    if "429" in msg or "too many" in msg:
        return "⏳ SoundCloud temporariamente sobrecarregado. Tente novamente em alguns minutos."
    if "403" in msg or "forbidden" in msg:
        return "🚫 SoundCloud bloqueou o acesso. Tente novamente mais tarde."
    if "sign in" in msg or "login" in msg:
        return "🔐 Esta faixa requer login no SoundCloud."
    if "no video" in msg or "no audio" in msg or "format" in msg:
        return "🎵 Formato de áudio não disponível para esta faixa."
    if "timeout" in msg or "timed out" in msg:
        return "⌛ Tempo esgotado ao conectar com o SoundCloud. Tente novamente."
    if "cancelad" in msg:
        return "❌ Download cancelado."
    return f"⚠️ Erro ao processar: {limpar_ansi(str(exc))[:120]}"

# ==============================================================================
# BUSCA DE LETRAS
# ==============================================================================
def _buscar_lrclib(titulo, artista):
    try:
        q = f"{artista} {titulo}".strip() if artista else titulo
        url = f"https://lrclib.net/api/search?q={urllib.parse.quote(q)}"
        req = urllib.request.Request(url, headers={"User-Agent": "SoundCloudMP3/1.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.load(r)
            if data and isinstance(data, list) and len(data) > 0:
                letra = data[0].get("plainLyrics") or data[0].get("syncedLyrics")
                if letra and len(letra.strip()) > 30:
                    return letra.strip()
    except Exception:
        pass
    return None

def _buscar_lyricsovh(titulo, artista):
    try:
        if not artista or not titulo:
            return None
        url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artista)}/{urllib.parse.quote(titulo)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.load(r)
            letra = data.get("lyrics")
            if letra and len(letra.strip()) > 30:
                return letra.strip()
    except Exception:
        pass
    return None

def _buscar_vagalume(titulo, artista):
    try:
        url = f"https://api.vagalume.com.br/search.php?art={urllib.parse.quote(artista)}&mus={urllib.parse.quote(titulo)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.load(r)
            if data.get("type") in ("exact", "aprox"):
                mus = data.get("mus", [])
                if mus and len(mus) > 0:
                    letra = mus[0].get("text")
                    if letra and len(letra.strip()) > 30:
                        return letra.strip()
    except Exception:
        pass
    return None

def buscar_letras_multi_fallback(titulo_raw, artista_raw=""):
    artista_sub, sep, titulo_sub = titulo_raw.partition(" - ")
    if sep:
        art, tit = artista_sub, titulo_sub
    else:
        art = artista_raw if artista_raw and artista_raw.lower() != "soundcloud" else ""
        tit = titulo_raw
    tit_limpo = limpar_titulo_para_busca(tit)
    art_limpo = limpar_titulo_para_busca(art)
    tarefas = [
        lambda: _buscar_lrclib(tit_limpo, art_limpo),
        lambda: _buscar_lyricsovh(tit_limpo, art_limpo),
        lambda: _buscar_vagalume(tit_limpo, art_limpo),
        lambda: _buscar_lrclib(tit_limpo, ""),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futuros = [executor.submit(fn) for fn in tarefas]
        for fut in concurrent.futures.as_completed(futuros):
            res = fut.result()
            if res:
                return res
    return None

# ==============================================================================
# MANIPULAÇÃO DE ARQUIVOS MP3
# ==============================================================================
def embutir_capa_url(arquivo, url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            dados = r.read()
            mime = r.headers.get_content_type() or "image/jpeg"
        audio = MP3(arquivo)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=dados))
        audio.save()
        return True
    except Exception:
        return False

def embutir_letra(arquivo, letra_texto):
    if not letra_texto:
        return False
    try:
        audio = MP3(arquivo)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("USLT")
        audio.tags.add(USLT(encoding=3, lang="XXX", desc="Lyrics", text=letra_texto))
        audio.save()
        return True
    except Exception:
        return False

def buscar_itunes_capa(titulo_raw, artista_raw=""):
    try:
        artista_sub, sep, titulo_sub = titulo_raw.partition(" - ")
        if sep:
            artista_busca, titulo_busca = artista_sub, titulo_sub
        else:
            artista_busca = artista_raw if artista_raw and artista_raw.lower() != "soundcloud" else ""
            titulo_busca = titulo_raw
        tit_limpo = limpar_titulo_para_busca(titulo_busca)
        art_limpo = limpar_titulo_para_busca(artista_busca)
        tentativas = []
        if art_limpo and tit_limpo:
            tentativas.append(f"{art_limpo} {tit_limpo}")
        if tit_limpo:
            tentativas.append(tit_limpo)
        palavras_tit = tit_limpo.split()
        if len(palavras_tit) > 2:
            tentativas.append(" ".join(palavras_tit[:3]))
        for termo in tentativas:
            if not termo or len(termo.strip()) < 2:
                continue
            url_api = f"https://itunes.apple.com/search?term={urllib.parse.quote(termo.strip())}&media=music&entity=song&limit=8"
            req = urllib.request.Request(url_api, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.load(r)
            qa = palavras(art_limpo); qt = palavras(tit_limpo)
            for res in data.get("results", []):
                rt = palavras(res.get("trackName", ""))
                if len(qt & rt) > 0:
                    art = res.get("artworkUrl100")
                    if art:
                        return {"capa": art.replace("100x100bb", "600x600bb"),
                                "detalhes": f"{res.get('artistName')} • {res.get('trackName')} ({res.get('collectionName', 'Single')})"}
        return None
    except Exception:
        return None

def corrigir_tags(arquivo):
    nome = os.path.splitext(os.path.basename(arquivo))[0]
    artista, sep, titulo = nome.partition(" - ")
    if not sep:
        artista, titulo = "", nome
    try:
        audio = MP3(arquivo)
        if audio.tags is None:
            audio.add_tags()
        audio['TIT2'] = TIT2(encoding=3, text=titulo)
        audio['TALB'] = TALB(encoding=3, text=titulo)
        if artista:
            audio['TPE1'] = TPE1(encoding=3, text=artista)
        audio.save()
        return True
    except Exception:
        return False

def salvar_no_historico(titulo, artista, url):
    historico = []
    if os.path.exists(ARQ_HISTORICO):
        try:
            with open(ARQ_HISTORICO, "r", encoding="utf-8") as f:
                historico = json.load(f)
        except Exception:
            historico = []
    historico.append({"titulo": titulo, "artista": artista, "url": url,
                      "data": datetime.now().strftime("%d/%m/%Y %H:%M")})
    with open(ARQ_HISTORICO, "w", encoding="utf-8") as f:
        json.dump(historico[-100:], f, ensure_ascii=False, indent=2)

# ==============================================================================
# FASTAPI APP
# ==============================================================================
app = FastAPI(title="SoundCloud MP3 Downloader")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# --- Health check ---
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "uptime": int(time.time()), "active_downloads": len(active_downloads),
            "queue_size": len(download_queue), "max_concurrent": MAX_CONCURRENT}

@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=500, detail="index.html não encontrado.")
    return FileResponse(index_path, media_type="text/html")

@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "name": "SoundCloud MP3 Downloader",
        "short_name": "SC MP3",
        "description": "Baixe músicas do SoundCloud em MP3 320kbps com capa HD e letras embutidas.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#06080f",
        "theme_color": "#ff6a00",
        "lang": "pt-BR",
        "categories": ["music", "utilities"],
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ]
    })

# --- Favicon PNG gerado dinamicamente (evita binários no repo) ---
ICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ff6a00"/>
      <stop offset="1" stop-color="#ff9d4d"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="112" fill="url(#g)"/>
  <g fill="white" transform="translate(96,140)">
    <rect x="0"   y="120" width="28" height="112" rx="14"/>
    <rect x="48"  y="80"  width="28" height="152" rx="14"/>
    <rect x="96"  y="40"  width="28" height="192" rx="14"/>
    <rect x="144" y="0"   width="28" height="232" rx="14"/>
    <rect x="192" y="60"  width="28" height="172" rx="14"/>
    <rect x="240" y="100" width="28" height="132" rx="14"/>
    <rect x="288" y="140" width="28" height="92"  rx="14"/>
  </g>
</svg>'''

@app.get("/icon-192.png")
@app.get("/icon-512.png")
@app.get("/favicon.ico")
async def icon():
    # Retorna SVG com content-type image/svg+xml — browsers aceitam como favicon
    return Response(content=ICON_SVG, media_type="image/svg+xml")

@app.get("/sw.js")
async def service_worker():
    sw_code = """
const CACHE = 'sc-mp3-v2';
const ASSETS = ['/', '/manifest.webmanifest', '/icon-192.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy)).catch(()=>{});
      return r;
    }).catch(() => caches.match(e.request).then(m => m || caches.match('/')))
  );
});
"""
    return Response(content=sw_code, media_type="application/javascript")

# --- Progresso + Cancelamento + Status da fila ---
@app.get("/api/progresso/{download_id}")
async def obter_progresso(download_id: str):
    prog = progresso_downloads.get(download_id, {"pct": 0, "status": "Iniciando..."}).copy()
    info = active_downloads.get(download_id, {})
    prog["queue_pos"] = info.get("queue_pos", 0)
    prog["queue_size"] = len(download_queue)
    prog["active_count"] = len([d for d in active_downloads.values() if d.get("started_at")])
    return prog

@app.post("/api/cancel/{download_id}")
async def cancelar_download(download_id: str):
    info = active_downloads.get(download_id)
    if not info:
        return {"ok": False, "msg": "Download não encontrado."}
    info["cancel"].set()
    progresso_downloads[download_id] = {"pct": 0, "status": "Cancelando..."}
    return {"ok": True, "msg": "Cancelamento solicitado."}

# --- Busca (com suporte a playlist/set do SoundCloud) ---
@app.post("/api/buscar")
@limiter.limit("30/minute")
async def buscar_faixas(request: Request, query: str = Form(...)):
    query = query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Digite uma busca válida.")

    if query.startswith("http"):
        try:
            query = validar_url_segura(query)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(query, download=False)
            # Playlist / Set
            entries = info.get("entries")
            if entries:
                resultados = []
                for ent in entries:
                    if not ent:
                        continue
                    url_ent = ent.get("webpage_url") or ent.get("url") or query
                    dur_seg = ent.get("duration")
                    thumb = ent.get("thumbnail") or info.get("thumbnail")
                    resultados.append({
                        "url": url_ent,
                        "titulo": ent.get("title", "Sem título"),
                        "artista": ent.get("uploader") or info.get("uploader", "Desconhecido"),
                        "duracao": ent.get("duration_string") or fmt_duracao(dur_seg),
                        "segundos": dur_seg,
                        "thumb": thumb,
                        "playlist": info.get("title"),
                    })
                return {"resultados": resultados, "playlist": info.get("title"), "total": len(resultados)}
            # Faixa única
            thumb = info.get("thumbnail") or obter_og_image(query)
            dur_seg = info.get("duration")
            return {"resultados": [{
                "url": query,
                "titulo": info.get("title", "Sem título"),
                "artista": info.get("uploader", "Desconhecido"),
                "duracao": info.get("duration_string") or fmt_duracao(dur_seg),
                "segundos": dur_seg,
                "thumb": thumb
            }]}
        except yt_dlp.utils.DownloadError as e:
            raise HTTPException(status_code=400, detail=erro_humano(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=erro_humano(e))

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
            info = ydl.extract_info(f"scsearch10:{query}", download=False)
            entradas = info.get("entries") or []
            raw_resultados = []
            urls_sem_capa = []
            for idx, ent in enumerate(entradas):
                if not ent:
                    continue
                url = ent.get("webpage_url") or ent.get("url")
                titulo = ent.get("title") or "Sem título"
                artista = ent.get("uploader") or "SoundCloud"
                dur_seg = ent.get("duration")
                dur_fmt = ent.get("duration_string") or fmt_duracao(dur_seg)
                thumb = ent.get("thumbnail")
                raw_resultados.append({"url": url, "titulo": titulo, "artista": artista,
                                       "duracao": dur_fmt, "segundos": dur_seg, "thumb": thumb})
                if not thumb and url:
                    urls_sem_capa.append((idx, url))
            if urls_sem_capa:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    futuros = {executor.submit(obter_og_image, u): i for i, u in urls_sem_capa}
                    for fut in concurrent.futures.as_completed(futuros):
                        idx = futuros[fut]
                        img_url = fut.result()
                        if img_url:
                            raw_resultados[idx]["thumb"] = img_url
            return {"resultados": raw_resultados}
    except Exception as e:
        raise HTTPException(status_code=500, detail=erro_humano(e))

@app.post("/api/consultar-capa")
@limiter.limit("30/minute")
async def consultar_capa(request: Request, titulo: str = Form(...), artista: str = Form("")):
    itunes_info = buscar_itunes_capa(titulo, artista)
    return {"itunes": itunes_info}

@app.post("/api/stream")
@limiter.limit("30/minute")
async def obter_stream(request: Request, url: str = Form(...)):
    try:
        url = validar_url_segura(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": "bestaudio"}) as ydl:
            info = ydl.extract_info(url, download=False)
            stream_url = None
            for f in info.get("formats", []):
                if f.get("protocol", "").startswith("http") and f.get("ext") in ("mp3", "m4a", "aac"):
                    stream_url = f.get("url")
                    break
            if not stream_url:
                stream_url = info.get("url")
            if not stream_url:
                raise Exception("Fluxo de áudio não encontrado.")
            return {"stream_url": stream_url}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=erro_humano(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=erro_humano(e))

# --- Download principal (com fila, cancelamento, SSRF protection) ---
@app.post("/api/download")
@limiter.limit("10/minute")
async def baixar_mp3(request: Request, url: str = Form(...), capa_custom: str = Form(None), download_id: str = Form("default")):
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL inválida.")
    try:
        url = validar_url_segura(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    register_download(download_id)
    baixados = []
    progresso_downloads[download_id] = {"pct": 5, "status": "Aguardando slot na fila..."}

    def hook(d):
        if is_cancelled(download_id):
            raise RuntimeError("Download cancelado pelo usuário.")
        if d.get("status") == "downloading":
            try:
                p_str = d.get("_percent_str", "0").replace("%", "").strip()
                p_val = int(float(p_str))
                progresso_downloads[download_id] = {"pct": min(int(p_val * 0.8), 80),
                                                    "status": f"Baixando stream: {p_str}%"}
            except Exception:
                pass
        elif d.get("status") == "finished":
            progresso_downloads[download_id] = {"pct": 85, "status": "Convertendo áudio para MP3 320kbps..."}
            caminho = d.get("filepath") or d.get("filename")
            info_dict = d.get("info_dict") or {}
            if caminho:
                baixados.append((os.path.splitext(caminho)[0] + ".mp3", info_dict.get("thumbnail"),
                                 info_dict.get("title"), info_dict.get("uploader")))

    opts = {
        "format": "bestaudio[protocol!^=m3u8]/bestaudio/best",
        "outtmpl": os.path.join(PASTA_MUSICAS, "%(uploader)s - %(title)s.%(ext)s"),
        "noplaylist": True,
        "postprocessors": [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': QUALIDADE_MP3},
            {'key': 'FFmpegMetadata'},
        ],
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
    }

    try:
        acquire_slot(download_id)
        if is_cancelled(download_id):
            raise RuntimeError("Download cancelado pelo usuário.")

        progresso_downloads[download_id] = {"pct": 10, "status": "Iniciando download da faixa..."}

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            titulo = info.get("title", "musica")
            artista = info.get("uploader", "")
            thumb = info.get("thumbnail") or obter_og_image(url)

        if is_cancelled(download_id):
            raise RuntimeError("Download cancelado pelo usuário.")

        progresso_downloads[download_id] = {"pct": 92, "status": "Consultando bases de letras e embutindo tags..."}

        arquivo_final = None
        for arq, _, _, _ in baixados:
            if os.path.exists(arq):
                arquivo_final = arq
                break
        if not arquivo_final:
            arquivos = glob.glob(os.path.join(PASTA_MUSICAS, f"*{titulo[:15]}*.mp3"))
            if arquivos:
                arquivo_final = arquivos[0]
        if not arquivo_final or not os.path.exists(arquivo_final):
            raise Exception("Não foi possível gerar o arquivo MP3.")

        corrigir_tags(arquivo_final)
        if capa_custom and capa_custom.startswith("http"):
            embutir_capa_url(arquivo_final, capa_custom)
        elif thumb:
            embutir_capa_url(arquivo_final, thumb)
        letras = buscar_letras_multi_fallback(titulo, artista)
        if letras:
            embutir_letra(arquivo_final, letras)

        salvar_no_historico(titulo, artista, url)
        nome_download = f"{artista} - {titulo}.mp3" if artista else f"{titulo}.mp3"
        nome_limpo = re.sub(r'[\\/*?:"<>|]', "", nome_download)

        progresso_downloads[download_id] = {"pct": 100, "status": "Download pronto!"}

        return FileResponse(
            path=arquivo_final,
            filename=nome_limpo,
            media_type="audio/mpeg",
            headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(nome_limpo)}"'}
        )
    except ValueError as e:
        progresso_downloads[download_id] = {"pct": 0, "status": str(e)}
        raise HTTPException(status_code=400, detail=str(e))
    except yt_dlp.utils.DownloadError as e:
        msg = erro_humano(e)
        progresso_downloads[download_id] = {"pct": 0, "status": msg}
        raise HTTPException(status_code=500, detail=msg)
    except Exception as e:
        msg = erro_humano(e)
        progresso_downloads[download_id] = {"pct": 0, "status": msg}
        raise HTTPException(status_code=500, detail=msg)
    finally:
        release_slot(download_id)

@app.get("/api/historico")
async def obter_historico():
    if os.path.exists(ARQ_HISTORICO):
        try:
            with open(ARQ_HISTORICO, "r", encoding="utf-8") as f:
                return {"historico": json.load(f)}
        except Exception:
            return {"historico": []}
    return {"historico": []}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
