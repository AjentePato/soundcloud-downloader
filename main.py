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
    if os.environ.get("AUTO_UPDATE_YTDLP", "").lower() != "true":
        return
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"], check=False, timeout=60, capture_output=True)
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
    if not texto: return ""
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", texto)
    t = re.sub(r"(?i)\b(prod\.|prod|feat\.|feat|ft\.|ft|official|audio|video|lyric|lyrics|sped up|slowed|reverb)\b", " ", t)
    t = re.sub(r"[^a-zA-Z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def fmt_duracao(segundos):
    if segundos is None: return ""
    try:
        seg = int(float(segundos))
        return f"{seg // 60}:{seg % 60:02d}"
    except: return ""

def obter_og_image(url):
    if not url: return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            html = r.read(250000).decode("utf-8", "ignore")
        m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        return m.group(1) if m else None
    except: return None

# ==============================================================================
# BUSCA DE LETRAS (Mantida igual)
# ==============================================================================
def _buscar_lrclib(titulo, artista):
    try:
        q = f"{artista} {titulo}".strip() if artista else titulo
        url = f"https://lrclib.net/api/search?q={urllib.parse.quote(q)}"
        req = urllib.request.Request(url, headers={"User-Agent": "SoundCloudMP3/1.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.load(r)
            if data and len(data) > 0:
                letra = data[0].get("plainLyrics") or data[0].get("syncedLyrics")
                if letra and len(letra.strip()) > 30: return letra.strip()
    except: pass
    return None

def _buscar_lyricsovh(titulo, artista):
    try:
        if not artista or not titulo: return None
        url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artista)}/{urllib.parse.quote(titulo)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.load(r)
            letra = data.get("lyrics")
            if letra and len(letra.strip()) > 30: return letra.strip()
    except: pass
    return None

def buscar_letras_multi_fallback(titulo_raw, artista_raw=""):
    artista_sub, sep, titulo_sub = titulo_raw.partition(" - ")
    art = artista_sub if sep else (artista_raw if artista_raw.lower() != "soundcloud" else "")
    tit = titulo_sub if sep else titulo_raw
    tit_limpo = limpar_titulo_para_busca(tit)
    art_limpo = limpar_titulo_para_busca(art)
    tarefas = [lambda: _buscar_lrclib(tit_limpo, art_limpo), lambda: _buscar_lyricsovh(tit_limpo, art_limpo), lambda: _buscar_lrclib(tit_limpo, "")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futuros = [executor.submit(fn) for fn in tarefas]
        for fut in concurrent.futures.as_completed(futuros):
            res = fut.result()
            if res: return res
    return None

# ==============================================================================
# MANIPULAÇÃO DE ARQUIVOS MP3 (Mantida igual)
# ==============================================================================
def embutir_capa_url(arquivo, url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            dados = r.read()
            mime = r.headers.get_content_type() or "image/jpeg"
        audio = MP3(arquivo)
        if audio.tags is None: audio.add_tags()
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=dados))
        audio.save()
        return True
    except: return False

def embutir_letra(arquivo, letra_texto):
    if not letra_texto: return False
    try:
        audio = MP3(arquivo)
        if audio.tags is None: audio.add_tags()
        audio.tags.delall("USLT")
        audio.tags.add(USLT(encoding=3, lang="XXX", desc="Lyrics", text=letra_texto))
        audio.save()
        return True
    except: return False

def buscar_itunes_capa(titulo_raw, artista_raw=""):
    try:
        artista_sub, sep, titulo_sub = titulo_raw.partition(" - ")
        artista_busca = artista_sub if sep else (artista_raw if artista_raw.lower() != "soundcloud" else "")
        titulo_busca = titulo_sub if sep else titulo_raw
        tit_limpo = limpar_titulo_para_busca(titulo_busca)
        art_limpo = limpar_titulo_para_busca(artista_busca)
        tentativas = [f"{art_limpo} {tit_limpo}"] if art_limpo and tit_limpo else []
        if tit_limpo: tentativas.append(tit_limpo)
        for termo in tentativas:
            if not termo or len(termo.strip()) < 2: continue
            url_api = f"https://itunes.apple.com/search?term={urllib.parse.quote(termo)}&media=music&entity=song&limit=5"
            req = urllib.request.Request(url_api, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.load(r)
            qt = palavras(tit_limpo)
            for res in data.get("results", []):
                rt = palavras(res.get("trackName", ""))
                if len(qt & rt) > 0:
                    art = res.get("artworkUrl100")
                    if art: return {"capa": art.replace("100x100bb", "600x600bb"), "detalhes": f"{res.get('artistName')} • {res.get('trackName')}"}
        return None
    except: return None

def corrigir_tags(arquivo):
    nome = os.path.splitext(os.path.basename(arquivo))[0]
    artista, sep, titulo = nome.partition(" - ")
    if not sep: artista, titulo = "", nome
    try:
        audio = MP3(arquivo)
        if audio.tags is None: audio.add_tags()
        audio['TIT2'] = TIT2(encoding=3, text=titulo)
        audio['TALB'] = TALB(encoding=3, text=titulo)
        if artista: audio['TPE1'] = TPE1(encoding=3, text=artista)
        audio.save()
        return True
    except: return False

def salvar_no_historico(titulo, artista, url):
    historico = []
    if os.path.exists(ARQ_HISTORICO):
        try:
            with open(ARQ_HISTORICO, "r", encoding="utf-8") as f: historico = json.load(f)
        except: historico = []
    historico.append({"titulo": titulo, "artista": artista, "url": url, "data": datetime.now().strftime("%d/%m/%Y %H:%M")})
    with open(ARQ_HISTORICO, "w", encoding="utf-8") as f:
        json.dump(historico[-100:], f, ensure_ascii=False, indent=2)

# ==============================================================================
# FASTAPI APP
# ==============================================================================
app = FastAPI(title="Multi-Source MP3 Downloader")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Disposition"])

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

@app.get("/manifest.json")
async def manifest():
    return {
        "name": "Multi-Source MP3 Downloader", "short_name": "MP3 Pro", "start_url": "/", "display": "standalone",
        "background_color": "#0b0f19", "theme_color": "#f97316",
        "icons": [{"src": "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=192&auto=format&fit=crop&q=80", "sizes": "192x192", "type": "image/jpeg"}]
    }

@app.get("/api/progresso/{download_id}")
async def obter_progresso(download_id: str):
    return progresso_downloads.get(download_id, {"pct": 0, "status": "Iniciando..."})

# --- BUSCA UNIFICADA E ESPECÍFICA ---

@app.post("/api/buscar")
async def buscar_faixas(query: str = Form(...)):
    """Busca padrão (SoundCloud)"""
    query = query.strip()
    if not query: raise HTTPException(400, "Digite uma busca válida.")
    if query.startswith("http"):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(query, download=False)
                return {"resultados": [{"url": query, "titulo": info.get("title"), "artista": info.get("uploader"), "duracao": info.get("duration_string") or fmt_duracao(info.get("duration")), "segundos": info.get("duration"), "thumb": info.get("thumbnail")}]}
        except Exception as e: raise HTTPException(400, detail=f"Erro no link: {limpar_ansi(str(e))}")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
            info = ydl.extract_info(f"scsearch10:{query}", download=False)
            entradas = info.get("entries") or []
            resultados = []
            for ent in entradas:
                if not ent: continue
                resultados.append({"url": ent.get("webpage_url") or ent.get("url"), "titulo": ent.get("title"), "artista": ent.get("uploader") or "SoundCloud", "duracao": ent.get("duration_string") or fmt_duracao(ent.get("duration")), "segundos": ent.get("duration"), "thumb": ent.get("thumbnail")})
            return {"resultados": resultados}
    except Exception as e: raise HTTPException(500, detail=f"Erro na busca: {limpar_ansi(str(e))}")

@app.post("/api/buscar-bandcamp")
async def buscar_bandcamp(query: str = Form(...)):
    """Busca específica no Bandcamp"""
    query = query.strip()
    if not query: raise HTTPException(400, "Digite uma busca válida.")
    if query.startswith("http"):
        # Se for link direto, usa a lógica genérica
        return await buscar_faixas(query) 
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
            # bcsearch é o extrator de busca do Bandcamp no yt-dlp
            info = ydl.extract_info(f"bcsearch10:{query}", download=False)
            entradas = info.get("entries") or []
            resultados = []
            for ent in entradas:
                if not ent: continue
                # Bandcamp geralmente traz o artista no uploader ou no título
                titulo = ent.get("title") or "Sem título"
                artista = ent.get("uploader") or "Bandcamp Artist"
                resultados.append({
                    "url": ent.get("webpage_url") or ent.get("url"),
                    "titulo": titulo,
                    "artista": artista,
                    "duracao": ent.get("duration_string") or fmt_duracao(ent.get("duration")),
                    "segundos": ent.get("duration"),
                    "thumb": ent.get("thumbnail")
                })
            return {"resultados": resultados}
    except Exception as e: raise HTTPException(500, detail=f"Erro na busca Bandcamp: {limpar_ansi(str(e))}")

@app.post("/api/buscar-archive")
async def buscar_archive(query: str = Form(...)):
    """Busca específica no Internet Archive"""
    query = query.strip()
    if not query: raise HTTPException(400, "Digite uma busca válida.")
    if query.startswith("http"):
        return await buscar_faixas(query)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
            # arcsearch é o extrator de busca do Archive.org
            info = ydl.extract_info(f"arcsearch10:{query}", download=False)
            entradas = info.get("entries") or []
            resultados = []
            for ent in entradas:
                if not ent: continue
                resultados.append({
                    "url": ent.get("webpage_url") or ent.get("url"),
                    "titulo": ent.get("title") or "Arquivo sem título",
                    "artista": ent.get("uploader") or "Internet Archive",
                    "duracao": ent.get("duration_string") or fmt_duracao(ent.get("duration")),
                    "segundos": ent.get("duration"),
                    "thumb": ent.get("thumbnail")
                })
            return {"resultados": resultados}
    except Exception as e: raise HTTPException(500, detail=f"Erro na busca Archive: {limpar_ansi(str(e))}")

@app.post("/api/consultar-capa")
async def consultar_capa(titulo: str = Form(...), artista: str = Form("")):
    return {"itunes": buscar_itunes_capa(titulo, artista)}

@app.post("/api/stream")
async def obter_stream(url: str = Form(...)):
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": "bestaudio"}) as ydl:
            info = ydl.extract_info(url, download=False)
            stream_url = next((f.get("url") for f in info.get("formats", []) if f.get("protocol", "").startswith("http") and f.get("ext") in ("mp3", "m4a", "aac")), info.get("url"))
            if not stream_url: raise Exception("Fluxo não encontrado.")
            return {"stream_url": stream_url}
    except Exception as e: raise HTTPException(400, detail=f"Erro ao obter prévia: {limpar_ansi(str(e))}")

@app.post("/api/download")
async def baixar_mp3(url: str = Form(...), capa_custom: str = Form(None), download_id: str = Form("default")):
    url = url.strip()
    if not url: raise HTTPException(400, "URL inválida.")
    baixados = []
    progresso_downloads[download_id] = {"pct": 10, "status": "Iniciando download..."}

    def hook(d):
        if d.get("status") == "downloading":
            try:
                p_str = d.get("_percent_str", "0").replace("%", "").strip()
                p_val = int(float(p_str))
                progresso_downloads[download_id] = {"pct": min(int(p_val * 0.8), 80), "status": f"Baixando: {p_str}%"}
            except: pass
        elif d.get("status") == "finished":
            progresso_downloads[download_id] = {"pct": 85, "status": "Convertendo para MP3 320kbps..."}
            caminho = d.get("filepath") or d.get("filename")
            info = d.get("info_dict") or {}
            if caminho: baixados.append((os.path.splitext(caminho)[0] + ".mp3", info.get("thumbnail"), info.get("title"), info.get("uploader")))

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(PASTA_MUSICAS, "%(uploader)s - %(title)s.%(ext)s"),
        "noplaylist": True,
        "postprocessors": [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': QUALIDADE_MP3}, {'key': 'FFmpegMetadata'}],
        "progress_hooks": [hook], "quiet": True, "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            titulo = info.get("title", "musica")
            artista = info.get("uploader", "")
            thumb = info.get("thumbnail") or obter_og_image(url)

        progresso_downloads[download_id] = {"pct": 92, "status": "Embutindo tags e letras..."}
        arquivo_final = next((arq for arq, _, _, _ in baixados if os.path.exists(arq)), None)
        if not arquivo_final:
            arquivos = glob.glob(os.path.join(PASTA_MUSICAS, f"*{titulo[:15]}*.mp3"))
            if arquivos: arquivo_final = arquivos[0]
        if not arquivo_final or not os.path.exists(arquivo_final): raise Exception("Falha ao gerar MP3.")

        corrigir_tags(arquivo_final)
        if capa_custom and capa_custom.startswith("http"): embutir_capa_url(arquivo_final, capa_custom)
        elif thumb: embutir_capa_url(arquivo_final, thumb)
        
        letras = buscar_letras_multi_fallback(titulo, artista)
        if letras: embutir_letra(arquivo_final, letras)

        salvar_no_historico(titulo, artista, url)
        nome_download = f"{artista} - {titulo}.mp3" if artista else f"{titulo}.mp3"
        nome_limpo = re.sub(r'[\\/*?:"<>|]', "", nome_download)
        progresso_downloads[download_id] = {"pct": 100, "status": "Pronto!"}
        return FileResponse(path=arquivo_final, filename=nome_limpo, media_type="audio/mpeg", headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(nome_limpo)}"'})
    except Exception as e:
        progresso_downloads[download_id] = {"pct": 0, "status": f"Erro: {str(e)}"}
        raise HTTPException(500, detail=f"Falha ao baixar: {limpar_ansi(str(e))}")

@app.get("/api/historico")
async def obter_historico():
    if os.path.exists(ARQ_HISTORICO):
        try:
            with open(ARQ_HISTORICO, "r", encoding="utf-8") as f: return {"historico": json.load(f)}
        except: return {"historico": []}
    return {"historico": []}
