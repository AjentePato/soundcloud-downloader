import os
import re
import glob
import json
import shutil
import urllib.request
import urllib.parse
import concurrent.futures
from threading import Lock, Thread
import time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
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
PASTA_MUSICAS = os.path.join(BASE_DIR, "Musicas_Temp")
ARQ_HISTORICO = os.path.join(BASE_DIR, "historico.json")
QUALIDADE_MP3 = "320"

os.makedirs(PASTA_MUSICAS, exist_ok=True)

historico_lock = Lock()
progresso_downloads = {}

STOP = {
    "the", "and", "for", "with", "official", "video", "lyric", "lyrics",
    "slowed", "reverb", "extended", "remix", "speed", "ultra", "super",
    "com", "sem", "pra", "pro", "uma", "não", "nao", "sped", "up", "edit"
}

# ==============================================================================
# RATE LIMITER
# ==============================================================================
limiter = Limiter(key_func=get_remote_address)
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

# ==============================================================================
# FUNÇÕES AUXILIARES
# ==============================================================================
def limpar_ansi(texto: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(texto or ""))

def palavras(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{2,}", (s or "").lower())} - STOP

def limpar_titulo_para_busca(texto: str) -> str:
    if not texto:
        return ""
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", texto)
    t = re.sub(r"(?i)\b(prod\.|prod|feat\.|feat|ft\.|ft|official|audio|video|lyric|lyrics|sped up|slowed|reverb)\b", " ", t)
    t = re.sub(r"[\^_\*~•★\-\|/\\:;<=>\?@#\$%&!\+\"]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def fmt_duracao(segundos) -> str:
    if segundos is None:
        return ""
    try:
        seg = int(float(segundos))
        return f"{seg // 60}:{seg % 60:02d}"
    except Exception:
        return ""

def remover_arquivo_seguro(caminho: str):
    """Limpeza garantida do disco em background."""
    try:
        if caminho and os.path.exists(caminho):
            os.remove(caminho)
    except Exception:
        pass

def limpar_progresso_antigo(download_id: str, delay_segundos: int = 120):
    """Evita acúmulo de chaves no dicionário de progresso."""
    def _expirar():
        time.sleep(delay_segundos)
        progresso_downloads.pop(download_id, None)

    Thread(target=_expirar, daemon=True).start()

def url_valida_http(url: str) -> bool:
    if not url:
        return False
    try:
        p = urllib.parse.urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

def obter_capa_soundcloud(url: str) -> Optional[str]:
    """
    Obtém a capa oficial do SoundCloud em alta resolução (500x500).
    Usa o oEmbed oficial como método primário e OpenGraph como fallback.
    """
    if not url_valida_http(url):
        return None

    # 1. Tenta via SoundCloud oEmbed (muito rápido, retorna ~500 bytes de JSON)
    try:
        oembed_url = f"https://soundcloud.com/oembed?format=json&url={urllib.parse.quote(url, safe=':/?=')}"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.load(r)
            thumb = data.get("thumbnail_url")
            if thumb:
                return thumb.replace("-large.jpg", "-t500x500.jpg")
    except Exception:
        pass

    # 2. Fallback: extração via meta tag og:image da página HTML
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=3) as r:
            html = r.read(150000).decode("utf-8", "ignore")
        m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html) or \
            re.search(r'<meta\s+content="([^"]+)"\s+property="og:image"', html)
        if m:
            return m.group(1).replace("-large.jpg", "-t500x500.jpg")
    except Exception:
        pass

    return None

# ==============================================================================
# BUSCA DE LETRAS
# ==============================================================================
def _buscar_lrclib(titulo: str, artista: str) -> Optional[str]:
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

def _buscar_lyricsovh(titulo: str, artista: str) -> Optional[str]:
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

def _buscar_vagalume(titulo: str, artista: str) -> Optional[str]:
    try:
        q = f"{artista} {titulo}".strip() if artista else titulo
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

def buscar_letras_multi_fallback(titulo_raw: str, artista_raw: str = "") -> Optional[str]:
    artista_sub, sep, titulo_sub = titulo_raw.partition(" - ")
    art = artista_sub if sep else (artista_raw if artista_raw.lower() != "soundcloud" else "")
    tit = titulo_sub if sep else titulo_raw

    tit_limpo = limpar_titulo_para_busca(tit)
    art_limpo = limpar_titulo_para_busca(art)

    tentativas = [
        lambda: _buscar_lrclib(tit_limpo, art_limpo),
        lambda: _buscar_lyricsovh(tit_limpo, art_limpo),
        lambda: _buscar_vagalume(tit_limpo, art_limpo),
        lambda: _buscar_lrclib(tit_limpo, ""),
    ]

    for fn in tentativas:
        resultado = fn()
        if resultado:
            return resultado
    return None

# ==============================================================================
# MANIPULAÇÃO DE ARQUIVOS MP3 E METADADOS
# ==============================================================================
def embutir_capa_url(arquivo: str, url: str) -> bool:
    if not url_valida_http(url):
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
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

def embutir_letra(arquivo: str, letra_texto: str) -> bool:
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

def buscar_itunes_capa(titulo_raw: str, artista_raw: str = ""):
    try:
        artista_sub, sep, titulo_sub = titulo_raw.partition(" - ")
        if sep:
            artista_busca = artista_sub
            titulo_busca = titulo_sub
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
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.load(r)

            qt = palavras(tit_limpo)

            for res in data.get("results", []):
                rt = palavras(res.get("trackName", ""))
                if len(qt & rt) > 0:
                    art = res.get("artworkUrl100")
                    if art:
                        return {
                            "capa": art.replace("100x100bb", "600x600bb"),
                            "detalhes": f"{res.get('artistName')} • {res.get('trackName')} ({res.get('collectionName', 'Single')})"
                        }
        return None
    except Exception:
        return None

def corrigir_tags(arquivo: str, titulo: str, artista: str) -> bool:
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

def salvar_no_historico(titulo: str, artista: str, url: str):
    with historico_lock:
        historico = []
        if os.path.exists(ARQ_HISTORICO):
            try:
                with open(ARQ_HISTORICO, "r", encoding="utf-8") as f:
                    historico = json.load(f)
            except Exception:
                historico = []
        historico.append({
            "titulo": titulo,
            "artista": artista,
            "url": url,
            "data": datetime.now().strftime("%d/%m/%Y %H:%M")
        })
        try:
            with open(ARQ_HISTORICO, "w", encoding="utf-8") as f:
                json.dump(historico[-100:], f, ensure_ascii=False, indent=2)
        except Exception:
            pass

# ==============================================================================
# ROTAS DA APLICAÇÃO
# ==============================================================================
@app.get("/", response_class=HTMLResponse)
def index():
    index_path = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="index.html não encontrado.")
    return FileResponse(index_path, media_type="text/html")

@app.get("/manifest.json")
def manifest():
    return {
        "name": "SoundCloud MP3 Downloader",
        "short_name": "SoundCloud MP3",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b0f19",
        "theme_color": "#f97316",
        "icons": [
            {
                "src": "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=192&auto=format&fit=crop&q=80",
                "sizes": "192x192",
                "type": "image/jpeg"
            },
            {
                "src": "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=512&auto=format&fit=crop&q=80",
                "sizes": "512x512",
                "type": "image/jpeg"
            }
        ]
    }

@app.get("/api/progresso/{download_id}")
def obter_progresso(download_id: str):
    return progresso_downloads.get(download_id, {"pct": 0, "status": "Iniciando..."})

@app.post("/api/buscar")
@limiter.limit("25/minute")
def buscar_faixas(request: Request, query: str = Form(...)):
    query = query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Digite uma busca válida.")

    opts = {"quiet": True, "no_warnings": True}

    if query.startswith("http"):
        if not url_valida_http(query):
            raise HTTPException(status_code=400, detail="URL inválida.")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
                thumb = info.get("thumbnail") or obter_capa_soundcloud(query)
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
            raise HTTPException(status_code=400, detail=f"Erro no link: {limpar_ansi(str(e))}")

    try:
        with yt_dlp.YoutubeDL({**opts, "extract_flat": True}) as ydl:
            info = ydl.extract_info(f"scsearch10:{query}", download=False)
            entradas = info.get("entries") or []

            resultados = []
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

                resultados.append({
                    "url": url,
                    "titulo": titulo,
                    "artista": artista,
                    "duracao": dur_fmt,
                    "segundos": dur_seg,
                    "thumb": thumb
                })

                # Adiciona para buscar capa em paralelo caso venha nula
                if not thumb and url:
                    urls_sem_capa.append((idx, url))

            # Busca todas as capas de uma só vez em paralelo
            if urls_sem_capa:
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(urls_sem_capa))) as executor:
                    futuros = {executor.submit(obter_capa_soundcloud, u): i for i, u in urls_sem_capa}
                    for fut in concurrent.futures.as_completed(futuros):
                        i = futuros[fut]
                        capa = fut.result()
                        if capa:
                            resultados[i]["thumb"] = capa

            return {"resultados": resultados}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na busca: {limpar_ansi(str(e))}")

@app.post("/api/consultar-capa")
def consultar_capa(titulo: str = Form(...), artista: str = Form("")):
    itunes_info = buscar_itunes_capa(titulo, artista)
    return {"itunes": itunes_info}

@app.post("/api/stream")
def obter_stream(url: str = Form(...)):
    if not url_valida_http(url):
        raise HTTPException(status_code=400, detail="URL inválida.")

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
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao obter prévia: {limpar_ansi(str(e))}")

@app.post("/api/download")
@limiter.limit("10/minute")
def baixar_mp3(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    capa_custom: Optional[str] = Form(None),
    download_id: str = Form("default")
):
    url = url.strip()
    if not url_valida_http(url):
        raise HTTPException(status_code=400, detail="URL inválida.")

    baixados = []
    progresso_downloads[download_id] = {"pct": 10, "status": "Iniciando download da faixa..."}

    def hook(d):
        if d.get("status") == "downloading":
            try:
                p_str = d.get("_percent_str", "0").replace("%", "").strip()
                p_val = int(float(p_str))
                progresso_downloads[download_id] = {
                    "pct": min(int(p_val * 0.8), 80),
                    "status": f"Baixando áudio: {p_str}%"
                }
            except Exception:
                pass
        elif d.get("status") == "finished":
            progresso_downloads[download_id] = {"pct": 85, "status": "Convertendo áudio (FFmpeg MP3 320kbps)..."}
            caminho = d.get("filepath") or d.get("filename")
            if caminho:
                baixados.append(os.path.splitext(caminho)[0] + ".mp3")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(PASTA_MUSICAS, f"{download_id}_%(id)s.%(ext)s"),
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
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            titulo = info.get("title", "musica")
            artista = info.get("uploader", "")
            thumb = info.get("thumbnail") or obter_capa_soundcloud(url)

        progresso_downloads[download_id] = {"pct": 92, "status": "Aplicando tags ID3 e letras..."}

        arquivo_final = None
        for arq in baixados:
            if os.path.exists(arq):
                arquivo_final = arq
                break

        if not arquivo_final:
            arquivos = glob.glob(os.path.join(PASTA_MUSICAS, f"{download_id}_*.mp3"))
            if arquivos:
                arquivo_final = arquivos[0]

        if not arquivo_final or not os.path.exists(arquivo_final):
            raise Exception("Não foi possível gerar o arquivo MP3.")

        corrigir_tags(arquivo_final, titulo, artista)

        if capa_custom and url_valida_http(capa_custom):
            embutir_capa_url(arquivo_final, capa_custom)
        elif thumb and url_valida_http(thumb):
            embutir_capa_url(arquivo_final, thumb)

        letras = buscar_letras_multi_fallback(titulo, artista)
        if letras:
            embutir_letra(arquivo_final, letras)

        salvar_no_historico(titulo, artista, url)

        nome_download = f"{artista} - {titulo}.mp3" if artista else f"{titulo}.mp3"
        nome_limpo = re.sub(r'[\\/*?:"<>|]', "", nome_download)
        nome_ascii = re.sub(r'[^\x20-\x7E]', '_', nome_limpo)
        nome_codificado = urllib.parse.quote(nome_limpo)

        progresso_downloads[download_id] = {"pct": 100, "status": "Download pronto!"}
        limpar_progresso_antigo(download_id)

        background_tasks.add_task(remover_arquivo_seguro, arquivo_final)

        return FileResponse(
            path=arquivo_final,
            filename=nome_limpo,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": f'attachment; filename="{nome_ascii}"; filename*=UTF-8\'\'{nome_codificado}'
            }
        )
    except Exception as e:
        progresso_downloads[download_id] = {"pct": 0, "status": f"Erro: {str(e)}"}
        limpar_progresso_antigo(download_id)
        for f in glob.glob(os.path.join(PASTA_MUSICAS, f"{download_id}_*")):
            remover_arquivo_seguro(f)
        raise HTTPException(status_code=500, detail=f"Falha ao baixar: {limpar_ansi(str(e))}")

@app.get("/api/historico")
def obter_historico():
    with historico_lock:
        if os.path.exists(ARQ_HISTORICO):
            try:
                with open(ARQ_HISTORICO, "r", encoding="utf-8") as f:
                    return {"historico": json.load(f)}
            except Exception:
                return {"historico": []}
    return {"historico": []}
