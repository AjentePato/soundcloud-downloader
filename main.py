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
from slowapi.errors import RateLimitExceeded
import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1, TALB, APIC, USLT

# ==============================================================================
# CONFIGURAÇÕES E LOGGING
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_MUSICAS = os.path.join(BASE_DIR, "Musicas")
ARQ_HISTORICO = os.path.join(BASE_DIR, "historico.json")
QUALIDADE_MP3 = "320"
DOMINIOS_PERMITIDOS = {
    "soundcloud.com", "m.soundcloud.com",
    "youtube.com", "www.youtube.com", 
    "youtu.be", "music.youtube.com"
}

os.makedirs(PASTA_MUSICAS, exist_ok=True)

# Rate Limiter
limiter = Limiter(key_func=get_remote_address)

# ==============================================================================
# FUNÇÕES AUXILIARES CORRIGIDAS
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
    """Regex corrigida e segura"""
    if not texto:
        return ""
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", texto)
    t = re.sub(r"(?i)\b(prod\.|prod|feat\.|feat|ft\.|ft|official|audio|video|lyric|lyrics|sped up|slowed|reverb)\b", " ", t)
    t = re.sub(r"[^a-zA-Z0-9\s]", " ", t)
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
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            html = r.read(250000).decode("utf-8", "ignore")
        m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        return m.group(1) if m else None
    except Exception as e:
        logger.warning(f"Falha ao obter og:image: {e}")
        return None

def validar_url(url: str) -> bool:
    """Segurança: Whitelist de domínios"""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc.lower() in DOMINIOS_PERMITIDOS
    except Exception:
        return False

# ==============================================================================
# BUSCA DE LETRAS (COM LOGS)
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
    except Exception as e:
        logger.warning(f"LRCLib falhou: {e}")
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
    except Exception as e:
        logger.warning(f"LyricsOVH falhou: {e}")
    return None

def buscar_letras_multi_fallback(titulo_raw, artista_raw=""):
    artista_sub, sep, titulo_sub = titulo_raw.partition(" - ")
    art = artista_sub if sep else (artista_raw if artista_raw.lower() != "soundcloud" else "")
    tit = titulo_sub if sep else titulo_raw

    tit_limpo = limpar_titulo_para_busca(tit)
    art_limpo = limpar_titulo_para_busca(art)

    tarefas = [
        lambda: _buscar_lrclib(tit_limpo, art_limpo),
        lambda: _buscar_lyricsovh(tit_limpo, art_limpo),
        lambda: _buscar_lrclib(tit_limpo, ""),
    ]

    with ThreadPoolExecutor(max_workers=3) as executor:
        futuros = [executor.submit(fn) for fn in tarefas]
        for fut in asyncio.as_completed(futuros):
            res = fut.result()
            if res:
                # Cancela os restantes implicitamente ao retornar
                return res
    return None

# ==============================================================================
# MANIPULAÇÃO DE MP3 E CAPAS
# ==============================================================================
def embutir_capa_url(arquivo, url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            # Limita tamanho para evitar OOM
            dados = r.read(5 * 1024 * 1024)  # Max 5MB
            mime = r.headers.get_content_type() or "image/jpeg"
        
        audio = MP3(arquivo)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=dados))
        audio.save()
        return True
    except Exception as e:
        logger.warning(f"Falha ao embutir capa: {e}")
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
    except Exception as e:
        logger.warning(f"Falha ao embutir letra: {e}")
        return False

def buscar_itunes_capa(titulo_raw, artista_raw=""):
    try:
        artista_sub, sep, titulo_sub = titulo_raw.partition(" - ")
        artista_busca = artista_sub if sep else (artista_raw if artista_raw.lower() != "soundcloud" else "")
        titulo_busca = titulo_sub if sep else titulo_raw

        tit_limpo = limpar_titulo_para_busca(titulo_busca)
        art_limpo = limpar_titulo_para_busca(artista_busca)

        tentativas = []
        if art_limpo and tit_limpo:
            tentativas.append(f"{art_limpo} {tit_limpo}")
        if tit_limpo:
            tentativas.append(tit_limpo)

        for termo in tentativas:
            if not termo or len(termo.strip()) < 2:
                continue
            url_api = f"https://itunes.apple.com/search?term={urllib.parse.quote(termo)}&media=music&entity=song&limit=5"
            req = urllib.request.Request(url_api, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.load(r)

            qt = palavras(tit_limpo)
            for res in data.get("results", []):
                rt = palavras(res.get("trackName", ""))
                if len(qt & rt) > 0:
                    art = res.get("artworkUrl100")
                    if art:
                        return {
                            "capa": art.replace("100x100bb", "600x600bb"),
                            "detalhes": f"{res.get('artistName')} • {res.get('trackName')}"
                        }
        return None
    except Exception as e:
        logger.warning(f"iTunes search falhou: {e}")
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
    except Exception as e:
        logger.warning(f"Falha ao corrigir tags: {e}")
        return False

def salvar_no_historico(titulo, artista, url):
    historico = []
    if os.path.exists(ARQ_HISTORICO):
        try:
            with open(ARQ_HISTORICO, "r", encoding="utf-8") as f:
                historico = json.load(f)
        except Exception:
            historico = []
    
    # Chaves SEM espaços extras
    historico.append({
        "titulo": titulo,
        "artista": artista,
        "url": url,
        "data": datetime.now().strftime("%d/%m/%Y %H:%M")
    })
    with open(ARQ_HISTORICO, "w", encoding="utf-8") as f:
        json.dump(historico[-100:], f, ensure_ascii=False, indent=2)

# ==============================================================================
# LÓGICA DE DOWNLOAD BLOQUEANTE (RODA EM THREAD)
# ==============================================================================
def _download_sync(url, opts, download_id, baixados_list):
    """Função síncrona que faz o trabalho pesado. Chamada via to_thread."""
    def hook(d):
        if d.get("status") == "downloading":
            try:
                p_str = d.get("_percent_str", "0").replace("%", "").strip()
                p_val = int(float(p_str))
                progresso_downloads[download_id] = {
                    "pct": min(int(p_val * 0.8), 80),
                    "status": f"Baixando stream: {p_str}%"
                }
            except Exception:
                pass
        elif d.get("status") == "finished":
            progresso_downloads[download_id] = {"pct": 85, "status": "Convertendo para MP3 320kbps..."}
            caminho = d.get("filepath") or d.get("filename")
            info = d.get("info_dict") or {}
            if caminho:
                baixados_list.append((
                    os.path.splitext(caminho)[0] + ".mp3",
                    info.get("thumbnail"),
                    info.get("title"),
                    info.get("uploader")
                ))

    opts["progress_hooks"] = [hook]
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)

# ==============================================================================
# FASTAPI APP
# ==============================================================================
app = FastAPI(title="SoundCloud & YouTube MP3 Downloader")
app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    raise HTTPException(status_code=429, detail="Muitas requisições. Aguarde um momento.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Em produção real, restrinja ao seu domínio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=500, detail="index.html não encontrado.")
    return FileResponse(index_path, media_type="text/html")

@app.get("/manifest.json")
async def manifest():
    return {
        "name": "SC/YT MP3 Downloader",
        "short_name": "MP3 Pro",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b0f19",
        "theme_color": "#f97316",
        "icons": [
            {"src": "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=192&auto=format&fit=crop&q=80", "sizes": "192x192", "type": "image/jpeg"},
            {"src": "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=512&auto=format&fit=crop&q=80", "sizes": "512x512", "type": "image/jpeg"}
        ]
    }

@app.post("/api/upload-cookies")
@limiter.limit("10/minute")
async def upload_cookies(request: Request, file: UploadFile = File(...)):
    """Upload seguro de cookies.txt por usuário"""
    if not file.filename.endswith('.txt'):
        raise HTTPException(400, "Apenas arquivos .txt são aceitos")
    
    content = await file.read()
    # Validação básica de formato Netscape
    if b"# Netscape HTTP Cookie File" not in content[:300] and b"httpOnly" not in content.lower():
        raise HTTPException(400, "Arquivo de cookies inválido. Exporte usando extensão 'Get cookies.txt LOCALLY'.")
    
    # Salva em temp dir com hash do IP para isolamento
    user_hash = hashlib.md5(request.client.host.encode()).hexdigest()[:10]
    cookie_path = os.path.join(tempfile.gettempdir(), f"yt_cookies_{user_hash}.txt")
    
    with open(cookie_path, 'wb') as f:
        f.write(content)
    
    logger.info(f"Cookies enviados por {request.client.host}")
    return {"cookie_file": cookie_path}

@app.get("/api/progresso/{download_id}")
async def obter_progresso(download_id: str):
    return progresso_downloads.get(download_id, {"pct": 0, "status": "Iniciando..."})

@app.post("/api/buscar")
@limiter.limit("30/minute")
async def buscar_faixas(request: Request, query: str = Form(...)):
    query = query.strip()
    if not query:
        raise HTTPException(400, "Digite uma busca válida.")

    if query.startswith("http"):
        if not validar_url(query):
            raise HTTPException(400, "Domínio não suportado. Use SoundCloud ou YouTube.")
        try:
            # Busca assíncrona de metadata
            info = await asyncio.to_thread(
                lambda: yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}).extract_info(query, download=False)
            )
            thumb = info.get("thumbnail") or obter_og_image(query)
            duracao_seg = info.get("duration")
            return {
                "resultados": [{
                    "url": query,
                    "titulo": info.get("title", "Sem título"),
                    "artista": info.get("uploader", "Desconhecido"),
                    "duracao": info.get("duration_string") or fmt_duracao(duracao_seg),
                    "segundos": duracao_seg,
                    "thumb": thumb
                }]
            }
        except Exception as e:
            raise HTTPException(400, detail=f"Erro no link: {limpar_ansi(str(e))}")

    # Busca textual apenas no SoundCloud (YouTube search requer API key ou cookies)
    try:
        info = await asyncio.to_thread(
            lambda: yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True})
                         .extract_info(f"scsearch10:{query}", download=False)
        )
        entradas = info.get("entries") or []
        raw_resultados = []
        
        for ent in entradas:
            if not ent:
                continue
            raw_resultados.append({
                "url": ent.get("webpage_url") or ent.get("url"),
                "titulo": ent.get("title") or "Sem título",
                "artista": ent.get("uploader") or "SoundCloud",
                "duracao": ent.get("duration_string") or fmt_duracao(ent.get("duration")),
                "segundos": ent.get("duration"),
                "thumb": ent.get("thumbnail")
            })
        return {"resultados": raw_resultados}
    except Exception as e:
        raise HTTPException(500, detail=f"Erro na busca: {limpar_ansi(str(e))}")

@app.post("/api/consultar-capa")
@limiter.limit("30/minute")
async def consultar_capa(request: Request, titulo: str = Form(...), artista: str = Form("")):
    itunes_info = buscar_itunes_capa(titulo, artista)
    return {"itunes": itunes_info}

@app.post("/api/stream")
@limiter.limit("20/minute")
async def obter_stream(request: Request, url: str = Form(...)):
    if not validar_url(url):
        raise HTTPException(400, "Domínio não suportado.")
    try:
        info = await asyncio.to_thread(
            lambda: yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": "bestaudio"})
                         .extract_info(url, download=False)
        )
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
    except Exception as e:
        raise HTTPException(400, detail=f"Erro ao obter prévia: {limpar_ansi(str(e))}")

@app.post("/api/download")
@limiter.limit("5/minute")  # Rate limit mais restritivo para downloads
async def baixar_mp3(
    request: Request,
    url: str = Form(...),
    capa_custom: str = Form(None),
    download_id: str = Form("default"),
    cookie_file: str = Form(None)
):
    url = url.strip()
    if not url or not validar_url(url):
        raise HTTPException(400, "URL inválida ou domínio não suportado.")

    # Verifica se o cookie file existe e pertence ao usuário (básico)
    if cookie_file:
        user_hash = hashlib.md5(request.client.host.encode()).hexdigest()[:10]
        expected_path = os.path.join(tempfile.gettempdir(), f"yt_cookies_{user_hash}.txt")
        if cookie_file != expected_path or not os.path.exists(cookie_file):
            logger.warning(f"Tentativa de uso de cookie inválido por {request.client.host}")
            cookie_file = None  # Fallback para download sem cookies

    baixados = []
    progresso_downloads[download_id] = {"pct": 10, "status": "Iniciando download da faixa..."}

    opts = {
        "format": "bestaudio[protocol!^=m3u8]/bestaudio/best",
        "outtmpl": os.path.join(PASTA_MUSICAS, "%(uploader)s - %(title)s.%(ext)s"),
        "noplaylist": True,
        "postprocessors": [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': QUALIDADE_MP3},
            {'key': 'FFmpegMetadata'},
        ],
        "quiet": True,
        "no_warnings": True,
    }
    
    if cookie_file:
        opts["cookiefile"] = cookie_file

    try:
        # EXECUÇÃO ASSÍNCRONA REAL DO YT-DLP
        info = await asyncio.to_thread(_download_sync, url, opts, download_id, baixados)
        
        titulo = info.get("title", "musica")
        artista = info.get("uploader", "")
        thumb = info.get("thumbnail") or obter_og_image(url)

        progresso_downloads[download_id] = {"pct": 92, "status": "Embutindo tags e letras..."}

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
    except Exception as e:
        logger.error(f"Falha no download {download_id}: {e}")
        progresso_downloads[download_id] = {"pct": 0, "status": f"Erro: {str(e)}"}
        raise HTTPException(500, detail=f"Falha ao baixar: {limpar_ansi(str(e))}")

@app.get("/api/historico")
async def obter_historico():
    if os.path.exists(ARQ_HISTORICO):
        try:
            with open(ARQ_HISTORICO, "r", encoding="utf-8") as f:
                return {"historico": json.load(f)}
        except Exception:
            return {"historico": []}
    return {"historico": []}
