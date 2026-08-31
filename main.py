import os
import re
import glob
import json
import sys
import subprocess
import urllib.request
import urllib.parse
import concurrent.futures
from datetime import datetime

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
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

# ==============================================================================
# AUTO-UPDATE DO YT-DLP (opcional)
# ==============================================================================
def atualizar_ytdlp_se_necessario():
    """
    Tenta atualizar o pacote yt-dlp via pip, se a variável AUTO_UPDATE_YTDLP
    estiver definida como 'true' no ambiente.
    """
    if os.environ.get("AUTO_UPDATE_YTDLP", "").lower() != "true":
        print("Auto-update do yt-dlp desativado.")
        return

    print("Verificando atualização do yt-dlp...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            check=False,
            timeout=60,
            capture_output=True,
            text=True
        )
        print("yt-dlp atualizado com sucesso (ou já estava na última versão).")
    except subprocess.TimeoutExpired:
        print("Timeout ao atualizar yt-dlp.")
    except Exception as e:
        print(f"Falha ao atualizar yt-dlp: {e}")

# Executa no startup
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
        minutos = seg // 60
        seg_rest = seg % 60
        return f"{minutos}:{seg_rest:02d}"
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

def buscar_letras_multi_fallback(titulo_raw, artista_raw=""):
    artista_sub, sep, titulo_sub = titulo_raw.partition(" - ")
    if sep:
        art = artista_sub
        tit = titulo_sub
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
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.load(r)

            qa = palavras(art_limpo)
            qt = palavras(tit_limpo)

            for res in data.get("results", []):
                ra = palavras(res.get("artistName", ""))
                rt = palavras(res.get("trackName", ""))
                score_tit = len(qt & rt)

                if score_tit > 0:
                    art = res.get("artworkUrl100")
                    if art:
                        return {
                            "capa": art.replace("100x100bb", "600x600bb"),
                            "detalhes": f"{res.get('artistName')} • {res.get('trackName')} ({res.get('collectionName', 'Single')})"
                        }
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
    historico.append({
        "titulo": titulo,
        "artista": artista,
        "url": url,
        "data": datetime.now().strftime("%d/%m/%Y %H:%M")
    })
    with open(ARQ_HISTORICO, "w", encoding="utf-8") as f:
        json.dump(historico[-100:], f, ensure_ascii=False, indent=2)

# ==============================================================================
# FASTAPI APP
# ==============================================================================
app = FastAPI(title="SoundCloud MP3 Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# Rota para o index.html
@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=500, detail="index.html não encontrado.")
    return FileResponse(index_path, media_type="text/html")

@app.get("/manifest.json")
async def manifest():
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
async def obter_progresso(download_id: str):
    return progresso_downloads.get(download_id, {"pct": 0, "status": "Iniciando..."})

@app.post("/api/buscar")
async def buscar_faixas(query: str = Form(...)):
    query = query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Digite uma busca válida.")

    if query.startswith("http"):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(query, download=False)
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
            raise HTTPException(status_code=400, detail=f"Erro no link: {limpar_ansi(str(e))}")

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

                raw_resultados.append({
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
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    futuros = {executor.submit(obter_og_image, u): i for i, u in urls_sem_capa}
                    for fut in concurrent.futures.as_completed(futuros):
                        idx = futuros[fut]
                        img_url = fut.result()
                        if img_url:
                            raw_resultados[idx]["thumb"] = img_url

            return {"resultados": raw_resultados}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na busca: {limpar_ansi(str(e))}")

@app.post("/api/consultar-capa")
async def consultar_capa(titulo: str = Form(...), artista: str = Form("")):
    itunes_info = buscar_itunes_capa(titulo, artista)
    return {"itunes": itunes_info}

@app.post("/api/stream")
async def obter_stream(url: str = Form(...)):
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
async def baixar_mp3(url: str = Form(...), capa_custom: str = Form(None), download_id: str = Form("default")):
    url = url.strip()
    if not url:
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
                    "status": f"Baixando stream: {p_str}%"
                }
            except Exception:
                pass
        elif d.get("status") == "finished":
            progresso_downloads[download_id] = {"pct": 85, "status": "Convertendo áudio para MP3 320kbps..."}
            caminho = d.get("filepath") or d.get("filename")
            info = d.get("info_dict") or {}
            if caminho:
                baixados.append((os.path.splitext(caminho)[0] + ".mp3", info.get("thumbnail"), info.get("title"), info.get("uploader")))

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
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            titulo = info.get("title", "musica")
            artista = info.get("uploader", "")
            thumb = info.get("thumbnail") or obter_og_image(url)

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
    except Exception as e:
        progresso_downloads[download_id] = {"pct": 0, "status": f"Erro: {str(e)}"}
        raise HTTPException(status_code=500, detail=f"Falha ao baixar: {limpar_ansi(str(e))}")

@app.get("/api/historico")
async def obter_historico():
    if os.path.exists(ARQ_HISTORICO):
        try:
            with open(ARQ_HISTORICO, "r", encoding="utf-8") as f:
                return {"historico": json.load(f)}
        except Exception:
            return {"historico": []}
    return {"historico": []}
