import os
import re
import glob
import json
import shutil
import asyncio
import time
import urllib.parse
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Form, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1, TALB, APIC, USLT

# ==============================================================================
# CONFIGURAÇÕES GERAIS E VERIFICAÇÕES
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_MUSICAS = os.path.join(BASE_DIR, "Musicas_Temp")
ARQ_HISTORICO = os.path.join(BASE_DIR, "historico.json")

os.makedirs(PASTA_MUSICAS, exist_ok=True)

# Verificação inicial do FFmpeg
if not shutil.which("ffmpeg"):
    print("[AVISO] FFmpeg não foi encontrado no PATH do sistema. Instale o FFmpeg para converter áudio quando necessário.")

historico_lock = asyncio.Lock()
progresso_downloads: Dict[str, Dict[str, Any]] = {}

STOP_WORDS = {
    "the", "and", "for", "with", "official", "video", "lyric", "lyrics",
    "slowed", "reverb", "extended", "remix", "speed", "ultra", "super",
    "com", "sem", "pra", "pro", "uma", "não", "nao", "sped", "up", "edit",
    "audio", "music", "original", "mix", "clip"
}

# ==============================================================================
# CLIENTE HTTP ASSÍNCRONO GLOBAL (ALTA VELOCIDADE COM CONNECTION POOL)
# ==============================================================================
http_client: Optional[httpx.AsyncClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
    http_client = httpx.AsyncClient(
        limits=limits,
        timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0),
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    )
    yield
    if http_client and not http_client.is_closed:
        await http_client.aclose()

async def get_http_client() -> httpx.AsyncClient:
    global http_client
    if http_client is None or http_client.is_closed:
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
        http_client = httpx.AsyncClient(
            limits=limits,
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            }
        )
    return http_client

# ==============================================================================
# RATE LIMITER E APLICAÇÃO FASTAPI
# ==============================================================================
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="SoundCloud MP3 Downloader Pro", lifespan=lifespan)
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
    return {w for w in re.findall(r"[a-z0-9]{2,}", (s or "").lower())} - STOP_WORDS

def limpar_titulo_para_busca(texto: str) -> str:
    if not texto:
        return ""
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", texto)
    t = re.sub(r"(?i)\b(prod\.|prod|feat\.|feat|ft\.|ft|official|audio|video|lyric|lyrics|sped up|slowed|reverb)\b", " ", t)
    t = re.sub(r'[\^_\*~•★\-\|/\\:;<=>\?@#\$%&!\+"]+', " ", t)
    return re.sub(r"\s+", " ", t).strip()

def fmt_duracao(segundos) -> str:
    if segundos is None:
        return ""
    try:
        seg = int(float(segundos))
        return f"{seg // 60}:{seg % 60:02d}"
    except Exception:
        return ""

def url_valida_http(url: str) -> bool:
    if not url:
        return False
    try:
        p = urllib.parse.urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

def limpar_pasta_temporaria(caminho_pasta: str):
    """Limpeza total e segura da subpasta isolada do download."""
    try:
        if caminho_pasta and os.path.exists(caminho_pasta):
            shutil.rmtree(caminho_pasta, ignore_errors=True)
    except Exception:
        pass

def limpar_progresso_antigo(download_id: str, delay_segundos: int = 90):
    """Remove o status do download da memória após intervalo seguro."""
    async def _expirar():
        await asyncio.sleep(delay_segundos)
        progresso_downloads.pop(download_id, None)

    asyncio.create_task(_expirar())

# ==============================================================================
# BUSCA DE CAPAS ASSÍNCRONA E ULTRA-RÁPIDA
# ==============================================================================
async def obter_capa_soundcloud(url: str) -> Optional[str]:
    """Obtém capa oficial em alta definição (500x500) via oEmbed com fallback OpenGraph."""
    if not url_valida_http(url):
        return None

    client = await get_http_client()

    # 1. oEmbed Oficial do SoundCloud (rápido e direto, ~150ms)
    try:
        oembed_url = f"https://soundcloud.com/oembed?format=json&url={urllib.parse.quote(url, safe=':/?=')}"
        res = await client.get(oembed_url, timeout=2.5)
        if res.status_code == 200:
            thumb = res.json().get("thumbnail_url")
            if thumb:
                return thumb.replace("-large.jpg", "-t500x500.jpg")
    except Exception:
        pass

    # 2. Fallback: meta tag og:image da página HTML
    try:
        res = await client.get(url, timeout=3.0)
        if res.status_code == 200:
            html = res.text
            m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html) or \
                re.search(r'<meta\s+content="([^"]+)"\s+property="og:image"', html)
            if m:
                return m.group(1).replace("-large.jpg", "-t500x500.jpg")
    except Exception:
        pass

    return None

async def buscar_itunes_capa(titulo_raw: str, artista_raw: str = "") -> Optional[Dict[str, str]]:
    """Consulta assíncrona com timeout rápido para Apple Music/iTunes."""
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

        client = await get_http_client()
        qt = palavras(tit_limpo)

        for termo in tentativas:
            if not termo or len(termo.strip()) < 2:
                continue

            url_api = f"https://itunes.apple.com/search?term={urllib.parse.quote(termo.strip())}&media=music&entity=song&limit=6"
            try:
                res = await client.get(url_api, timeout=3.0)
                if res.status_code != 200:
                    continue
                data = res.json()
            except Exception:
                continue

            for item in data.get("results", []):
                rt = palavras(item.get("trackName", ""))
                if len(qt & rt) > 0:
                    art = item.get("artworkUrl100")
                    if art:
                        return {
                            "capa": art.replace("100x100bb", "600x600bb"),
                            "detalhes": f"{item.get('artistName')} • {item.get('trackName')} ({item.get('collectionName', 'Single')})"
                        }
        return None
    except Exception:
        return None

# ==============================================================================
# BUSCA DE LETRAS OTIMIZADA COM EXECUÇÃO SIMULTÂNEA (LRCLIB + VAGALUME)
# ==============================================================================
async def _buscar_lrclib_exato(titulo: str, artista: str, duracao_seg: Optional[int]) -> Optional[Dict[str, str]]:
    if not titulo or not artista:
        return None
    try:
        client = await get_http_client()
        params = {"track_name": titulo, "artist_name": artista}
        if duracao_seg:
            params["duration"] = str(int(duracao_seg))
        res = await client.get("https://lrclib.net/api/get", params=params, timeout=2.5)
        if res.status_code == 200:
            data = res.json()
            synced = data.get("syncedLyrics")
            plain = data.get("plainLyrics")
            if synced and len(synced.strip()) > 30:
                return {"letra": synced.strip(), "tipo": "sincronizada", "fonte": "LRCLIB (.LRC)"}
            if plain and len(plain.strip()) > 30:
                return {"letra": plain.strip(), "tipo": "tradicional", "fonte": "LRCLIB"}
    except Exception:
        pass
    return None

async def _buscar_lrclib_search(query: str) -> Optional[Dict[str, str]]:
    if not query:
        return None
    try:
        client = await get_http_client()
        url = f"https://lrclib.net/api/search?q={urllib.parse.quote(query)}"
        res = await client.get(url, timeout=2.5)
        if res.status_code == 200:
            itens = res.json()
            if isinstance(itens, list) and len(itens) > 0:
                for item in itens:
                    synced = item.get("syncedLyrics")
                    if synced and len(synced.strip()) > 30:
                        return {"letra": synced.strip(), "tipo": "sincronizada", "fonte": "LRCLIB (.LRC)"}
                primeiro = itens[0]
                plain = primeiro.get("plainLyrics")
                if plain and len(plain.strip()) > 30:
                    return {"letra": plain.strip(), "tipo": "tradicional", "fonte": "LRCLIB"}
    except Exception:
        pass
    return None

async def _buscar_vagalume(titulo: str, artista: str) -> Optional[Dict[str, str]]:
    if not artista or not titulo:
        return None
    try:
        client = await get_http_client()
        url = f"https://api.vagalume.com.br/search.php?art={urllib.parse.quote(artista)}&mus={urllib.parse.quote(titulo)}"
        res = await client.get(url, timeout=2.5)
        if res.status_code == 200:
            data = res.json()
            if data.get("type") in ("exact", "aprox"):
                mus = data.get("mus", [])
                if mus and len(mus) > 0:
                    letra = mus[0].get("text")
                    if letra and len(letra.strip()) > 30:
                        return {"letra": letra.strip(), "tipo": "tradicional", "fonte": "Vagalume"}
    except Exception:
        pass
    return None

async def _buscar_lyricsovh(titulo: str, artista: str) -> Optional[Dict[str, str]]:
    if not artista or not titulo:
        return None
    try:
        client = await get_http_client()
        url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artista)}/{urllib.parse.quote(titulo)}"
        res = await client.get(url, timeout=2.0)
        if res.status_code == 200:
            letra = res.json().get("lyrics")
            if letra and len(letra.strip()) > 30:
                return {"letra": letra.strip(), "tipo": "tradicional", "fonte": "Lyrics.ovh"}
    except Exception:
        pass
    return None

async def buscar_letras_multi_fallback(
    titulo_raw: str,
    artista_raw: str = "",
    duracao_seg: Optional[int] = None
) -> Optional[Dict[str, str]]:
    """Busca concorrente em paralelo: LRCLIB (exato + busca) e Vagalume finalizando em < 800ms."""
    if not titulo_raw:
        return None

    artista_sub, sep, titulo_sub = titulo_raw.partition(" - ")
    art = artista_sub if sep else (artista_raw if artista_raw.lower() != "soundcloud" else "")
    tit = titulo_sub if sep else titulo_raw

    tit_limpo = limpar_titulo_para_busca(tit)
    art_limpo = limpar_titulo_para_busca(art)

    tarefas = []

    if art_limpo and tit_limpo:
        tarefas.append(_buscar_lrclib_exato(tit_limpo, art_limpo, duracao_seg))

    query_completa = f"{art_limpo} {tit_limpo}".strip() if art_limpo else tit_limpo
    if query_completa:
        tarefas.append(_buscar_lrclib_search(query_completa))

    if art_limpo and tit_limpo:
        tarefas.append(_buscar_vagalume(tit_limpo, art_limpo))

    if tarefas:
        resultados = await asyncio.gather(*tarefas, return_exceptions=True)

        for r in resultados:
            if isinstance(r, dict) and r.get("tipo") == "sincronizada":
                return r

        for r in resultados:
            if isinstance(r, dict) and r.get("letra"):
                return r

    if tit_limpo and tit_limpo != query_completa:
        res_tit = await _buscar_lrclib_search(tit_limpo)
        if res_tit:
            return res_tit

    return None

# ==============================================================================
# MANIPULAÇÃO DE TAGS E ARQUIVOS MP3 (MUTAGEN)
# ==============================================================================
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

def embutir_letra(arquivo: str, letra_info: Dict[str, str]) -> bool:
    letra_texto = letra_info.get("letra", "")
    if not letra_texto:
        return False
    try:
        audio = MP3(arquivo)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("USLT")
        desc = "Lyrics (LRC)" if letra_info.get("tipo") == "sincronizada" else "Lyrics"
        audio.tags.add(USLT(encoding=3, lang="XXX", desc=desc, text=letra_texto))
        audio.save()
        return True
    except Exception:
        return False

async def embutir_capa_url(arquivo: str, url: str) -> bool:
    if not url_valida_http(url):
        return False
    try:
        client = await get_http_client()
        res = await client.get(url, timeout=8.0)
        if res.status_code != 200:
            return False

        dados = res.content
        mime = res.headers.get("content-type", "image/jpeg").split(";")[0]

        def _salvar():
            audio = MP3(arquivo)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=dados))
            audio.save()

        await asyncio.to_thread(_salvar)
        return True
    except Exception:
        return False

async def salvar_no_historico(titulo: str, artista: str, url: str, qualidade: str = ""):
    async with historico_lock:
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
            "qualidade": qualidade,
            "data": datetime.now().strftime("%d/%m/%Y %H:%M")
        })
        try:
            with open(ARQ_HISTORICO, "w", encoding="utf-8") as f:
                json.dump(historico[-100:], f, ensure_ascii=False, indent=2)
        except Exception:
            pass

# ==============================================================================
# ROTAS DA APLICAÇÃO FASTAPI
# ==============================================================================
@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="index.html não encontrado.")
    return FileResponse(index_path, media_type="text/html")

@app.get("/manifest.json")
async def manifest():
    return {
        "name": "SoundCloud MP3 Downloader Pro",
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

# Stream de Progresso em Tempo Real com Server-Sent Events (SSE)
@app.get("/api/progresso/stream/{download_id}")
async def stream_progresso(download_id: str):
    clean_id = re.sub(r"[^a-zA-Z0-9_\-]", "", download_id)

    async def event_generator():
        ultimo_pct = -1
        ultimo_status = ""
        tentativas_sem_mudanca = 0
        while tentativas_sem_mudanca < 500:
            dados = progresso_downloads.get(clean_id)
            if dados:
                pct = dados.get("pct", 0)
                status = dados.get("status", "")
                if pct != ultimo_pct or status != ultimo_status:
                    ultimo_pct = pct
                    ultimo_status = status
                    tentativas_sem_mudanca = 0
                    yield f"data: {json.dumps(dados, ensure_ascii=False)}\n\n"
                    if pct >= 100 or "Erro" in status:
                        break
                else:
                    tentativas_sem_mudanca += 1
            else:
                tentativas_sem_mudanca += 1
            await asyncio.sleep(0.2)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# Fallback tradicional de polling
@app.get("/api/progresso/{download_id}")
async def obter_progresso(download_id: str):
    clean_id = re.sub(r"[^a-zA-Z0-9_\-]", "", download_id)
    return progresso_downloads.get(clean_id, {"pct": 0, "status": "Iniciando..."})

@app.post("/api/buscar")
@limiter.limit("25/minute")
async def buscar_faixas(request: Request, query: str = Form(...)):
    query = query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Digite uma busca válida.")

    opts = {"quiet": True, "no_warnings": True}

    if query.startswith("http"):
        if not url_valida_http(query):
            raise HTTPException(status_code=400, detail="URL inválida.")
        try:
            def _extract():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(query, download=False)

            info = await asyncio.to_thread(_extract)
            duracao_seg = info.get("duration")
            thumb = info.get("thumbnail") or await obter_capa_soundcloud(query)
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
        def _search():
            with yt_dlp.YoutubeDL({**opts, "extract_flat": True}) as ydl:
                return ydl.extract_info(f"scsearch10:{query}", download=False)

        info = await asyncio.to_thread(_search)
        entradas = info.get("entries") or []

        resultados: List[Dict[str, Any]] = []
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

            if not thumb and url:
                urls_sem_capa.append((idx, url))

        if urls_sem_capa:
            tarefas_capas = [obter_capa_soundcloud(u) for _, u in urls_sem_capa]
            capas_resolvidas = await asyncio.gather(*tarefas_capas, return_exceptions=True)
            for (idx, _), capa in zip(urls_sem_capa, capas_resolvidas):
                if isinstance(capa, str) and capa:
                    resultados[idx]["thumb"] = capa

        return {"resultados": resultados}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na busca: {limpar_ansi(str(e))}")

@app.post("/api/consultar-capa")
async def consultar_capa(titulo: str = Form(...), artista: str = Form("")):
    itunes_info = await buscar_itunes_capa(titulo, artista)
    return {"itunes": itunes_info}

@app.post("/api/stream")
async def obter_stream(url: str = Form(...)):
    url = url.strip()
    if not url_valida_http(url):
        raise HTTPException(status_code=400, detail="URL inválida.")

    try:
        def _get_stream():
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": "bestaudio"}) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.to_thread(_get_stream)
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
async def baixar_mp3(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    titulo: Optional[str] = Form(None),
    artista: Optional[str] = Form(None),
    duracao: Optional[str] = Form(None),
    capa_custom: Optional[str] = Form(None),
    download_id: str = Form("default"),
    qualidade: str = Form("auto")  # Padrão: "auto" (Ultra Rápido, sem re-encode desnecessário)
):
    url = url.strip()
    if not url_valida_http(url):
        raise HTTPException(status_code=400, detail="URL inválida.")

    # Converte duração de forma segura
    duracao_seg_val = None
    if duracao:
        try:
            duracao_seg_val = int(float(str(duracao).strip()))
        except (ValueError, TypeError):
            duracao_seg_val = None

    # 1. PIPELINE DE LETRAS EM PARALELO (Inicia IMEDIATAMENTE no milissegundo 0)
    tarefa_letras = None
    if titulo:
        tarefa_letras = asyncio.create_task(
            buscar_letras_multi_fallback(titulo, artista or "", duracao_seg_val)
        )

    # 2. Isolamento por pasta exclusiva (Prevenção de Path Traversal)
    clean_id = re.sub(r"[^a-zA-Z0-9_\-]", "", download_id or "") or f"dl_{int(time.time() * 1000)}"
    pasta_tarefa = os.path.join(PASTA_MUSICAS, clean_id)
    os.makedirs(pasta_tarefa, exist_ok=True)

    # 3. OTIMIZAÇÃO CRÍTICA DE CODIFICAÇÃO E SELEÇÃO DE FLUXO
    mapa_qualidades = {
        "auto": {"codec": None, "label": "Original Direto (Ultra Rápido - 0s espera)"},
        "320": {"codec": "320", "label": "320kbps CBR (Bitrate Máximo)"},
        "vbr0": {"codec": "0", "label": "VBR 0 (~240kbps - Alta Fidelidade)"},
        "192": {"codec": "192", "label": "192kbps CBR (Balanceado)"},
        "128": {"codec": "128", "label": "128kbps CBR (Leve)"},
    }
    cfg_qualidade = mapa_qualidades.get(qualidade, mapa_qualidades["auto"])

    progresso_downloads[clean_id] = {
        "pct": 15,
        "status": f"Iniciando download ({cfg_qualidade['label']})...",
        "detalhe": "Baixando stream direto"
    }

    def hook(d):
        if d.get("status") == "downloading":
            try:
                p_str = d.get("_percent_str", "0").replace("%", "").strip()
                p_val = int(float(p_str))
                progresso_downloads[clean_id] = {
                    "pct": min(int(p_val * 0.7) + 15, 80),
                    "status": f"Baixando stream: {p_str}%",
                    "detalhe": f"Velocidade: {d.get('_speed_str', 'N/A')}"
                }
            except Exception:
                pass
        elif d.get("status") == "finished":
            status_proc = "Finalizando MP3 original..." if cfg_qualidade['codec'] is None else f"Codificando áudio (FFmpeg MP3 {cfg_qualidade['label']})..."
            progresso_downloads[clean_id] = {
                "pct": 85,
                "status": status_proc,
                "detalhe": "Processando áudio"
            }

    # SELETOR DE STREAM TURBO:
    # Prioriza formatos nativos MP3 do SoundCloud (http_mp3_128 e hls_mp3_128)
    # Evita que o yt-dlp escolha Opus desnecessariamente, eliminando a recodificação lenta no Render!
    fmt_stream = "http_mp3_128/hls_mp3_128/bestaudio[acodec=mp3]/bestaudio[acodec^=mp3]/bestaudio[ext=mp3]/bestaudio/best"

    postprocessors = [
        {
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': cfg_qualidade['codec'],
            'nopostoverwrites': False
        }
        # FFmpegMetadata removido: o mutagen grava as tags ID3 completas em 5ms sem reescrever o arquivo com FFmpeg!
    ]

    opts = {
        "format": fmt_stream,
        "outtmpl": os.path.join(pasta_tarefa, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,  # Baixa múltiplos pedaços de HLS simultaneamente em paralelo!
        "postprocessors": postprocessors,
        "postprocessor_args": ["-threads", "0", "-compression_level", "9"],
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
    }

    try:
        def _executar_download():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = await asyncio.to_thread(_executar_download)
        titulo_final = titulo or info.get("title", "musica")
        artista_final = artista or info.get("uploader", "")
        duracao_seg = duracao or info.get("duration")

        progresso_downloads[clean_id] = {
            "pct": 88,
            "status": "Stream pronto! Aplicando metadados ID3...",
            "detalhe": "Finalizando áudio"
        }

        # Localiza o arquivo gerado na pasta isolada
        arquivos_mp3 = glob.glob(os.path.join(pasta_tarefa, "*.mp3")) + glob.glob(os.path.join(pasta_tarefa, "*.MP3"))
        if not arquivos_mp3:
            todos_arquivos = glob.glob(os.path.join(pasta_tarefa, "*.*"))
            if todos_arquivos:
                arquivo_final = todos_arquivos[0]
            else:
                raise Exception("Não foi possível gerar o arquivo de áudio.")
        else:
            arquivo_final = arquivos_mp3[0]

        # 1. Tags básicas ID3 em memória com mutagen (5ms)
        corrigir_tags(arquivo_final, titulo_final, artista_final)

        # 2. Capa em alta definição (só faz requisição se capa_custom não tiver sido enviada)
        capa_alvo = capa_custom if (capa_custom and url_valida_http(capa_custom)) else None
        if not capa_alvo:
            thumb = info.get("thumbnail")
            if not thumb:
                thumb = await obter_capa_soundcloud(url)
            if thumb and url_valida_http(thumb):
                capa_alvo = thumb

        if capa_alvo:
            progresso_downloads[clean_id] = {
                "pct": 92,
                "status": "Embutindo arte de capa oficial HD...",
                "detalhe": "Tag ID3 APIC"
            }
            await embutir_capa_url(arquivo_final, capa_alvo)

        # 3. LETRAS (Obtidas da tarefa que já rodou em paralelo durante o download)
        progresso_downloads[clean_id] = {
            "pct": 96,
            "status": "Embutindo letras sincronizadas...",
            "detalhe": "Tag ID3 USLT"
        }
        if tarefa_letras is None:
            dur_info = info.get("duration")
            dur_calc = duracao_seg_val or (int(float(dur_info)) if dur_info else None)
            tarefa_letras = asyncio.create_task(
                buscar_letras_multi_fallback(titulo_final, artista_final, dur_calc)
            )

        letra_info = await tarefa_letras
        status_letra = "Sem letra"
        if letra_info:
            embutir_letra(arquivo_final, letra_info)
            status_letra = f"Letra {letra_info['tipo'].capitalize()} ({letra_info['fonte']})"

        # 4. Grava no Histórico
        await salvar_no_historico(titulo_final, artista_final, url, cfg_qualidade['label'])

        nome_download = f"{artista_final} - {titulo_final}.mp3" if artista_final else f"{titulo_final}.mp3"
        nome_limpo = re.sub(r'[\\/*?:"<>|]', "", nome_download)
        nome_ascii = re.sub(r'[^\x20-\x7E]', '_', nome_limpo)
        nome_codificado = urllib.parse.quote(nome_limpo)

        progresso_downloads[clean_id] = {
            "pct": 100,
            "status": f"✓ Concluído em alta velocidade! {status_letra}",
            "detalhe": f"{cfg_qualidade['label']}"
        }
        limpar_progresso_antigo(clean_id)

        # Remoção segura da pasta temporária após envio ao cliente
        background_tasks.add_task(limpar_pasta_temporaria, pasta_tarefa)

        return FileResponse(
            path=arquivo_final,
            filename=nome_limpo,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": f'attachment; filename="{nome_ascii}"; filename*=UTF-8\'\'{nome_codificado}'
            }
        )
    except Exception as e:
        progresso_downloads[clean_id] = {
            "pct": 0,
            "status": f"Erro: {str(e)}",
            "detalhe": "Falha no download"
        }
        limpar_progresso_antigo(clean_id)
        limpar_pasta_temporaria(pasta_tarefa)
        raise HTTPException(status_code=500, detail=f"Falha ao processar download: {limpar_ansi(str(e))}")

@app.get("/api/historico")
async def obter_historico():
    async with historico_lock:
        if os.path.exists(ARQ_HISTORICO):
            try:
                with open(ARQ_HISTORICO, "r", encoding="utf-8") as f:
                    return {"historico": json.load(f)}
            except Exception:
                return {"historico": []}
    return {"historico": []}