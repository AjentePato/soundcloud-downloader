import os
import re
import glob
import json
import urllib.request
import urllib.parse
from datetime import datetime
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1, TALB, APIC

# Configurações de pastas universais (compatível com Linux, Windows e Nuvem)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_MUSICAS = os.path.join(BASE_DIR, "Musicas")
ARQ_HISTORICO = os.path.join(BASE_DIR, "historico.json")
QUALIDADE_MP3 = "320"

os.makedirs(PASTA_MUSICAS, exist_ok=True)

STOP = {"the", "and", "for", "with", "official", "video", "lyric", "lyrics",
        "slowed", "reverb", "extended", "remix", "speed", "ultra", "super",
        "com", "sem", "pra", "pro", "uma", "não", "nao"}

def palavras(s):
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower())} - STOP

def limpar_ansi(texto):
    return re.sub(r"\x1b\[[0-9;]*m", "", texto)

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

def buscar_itunes_capa(nome):
    try:
        artista, sep, titulo = nome.partition(" - ")
        termo = urllib.parse.quote(f"{artista} {titulo}" if sep else nome)
        url_api = f"https://itunes.apple.com/search?term={termo}&media=music&entity=song&limit=5"
        req = urllib.request.Request(url_api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        qa = palavras(artista)
        qt = palavras(titulo if sep else nome)
        for res in data.get("results", []):
            ra = palavras(res.get("artistName"))
            rt = palavras(res.get("trackName"))
            score = (len(qa & ra) + len(qt & rt)) if sep else len(qt & (ra | rt))
            if score > 0:
                art = res.get("artworkUrl100")
                if art:
                    return art.replace("100x100bb", "600x600bb")
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

# Inicialização do FastAPI
app = FastAPI(title="SoundCloud MP3 Downloader")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/buscar")
async def buscar_faixas(query: str = Form(...)):
    query = query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Digite uma busca válida.")
    
    # Se for link direto do SoundCloud
    if query.startswith("http"):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(query, download=False)
                return {
                    "resultados": [{
                        "url": query,
                        "titulo": info.get("title", "Sem título"),
                        "artista": info.get("uploader", "Desconhecido"),
                        "duracao": info.get("duration_string", ""),
                        "thumb": info.get("thumbnail")
                    }]
                }
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erro no link: {limpar_ansi(str(e))}")

    # Pesquisa de 10 faixas no SoundCloud
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
            info = ydl.extract_info(f"scsearch10:{query}", download=False)
            entradas = info.get("entries") or []
            resultados = []
            for ent in entradas:
                if not ent:
                    continue
                resultados.append({
                    "url": ent.get("webpage_url") or ent.get("url"),
                    "titulo": ent.get("title") or "Sem título",
                    "artista": ent.get("uploader") or "SoundCloud",
                    "duracao": ent.get("duration_string") or "",
                    "thumb": ent.get("thumbnail")
                })
            return {"resultados": resultados}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na busca: {limpar_ansi(str(e))}")

@app.post("/api/download")
async def baixar_mp3(url: str = Form(...)):
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL inválida.")

    baixados = []
    def hook(d):
        if d.get("status") == "finished":
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
            thumb = info.get("thumbnail")

        # Localiza o arquivo .mp3 gerado
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

        # Aplica tags e capa inteligente (iTunes ou SoundCloud)
        corrigir_tags(arquivo_final)
        nome_faixa = os.path.splitext(os.path.basename(arquivo_final))[0]
        capa_itunes = buscar_itunes_capa(nome_faixa)
        if capa_itunes:
            embutir_capa_url(arquivo_final, capa_itunes)
        elif thumb:
            embutir_capa_url(arquivo_final, thumb)

        salvar_no_historico(titulo, artista, url)
        nome_download = f"{artista} - {titulo}.mp3" if artista else f"{titulo}.mp3"
        nome_limpo = re.sub(r'[\\/*?:"<>|]', "", nome_download)

        return FileResponse(
            path=arquivo_final,
            filename=nome_limpo,
            media_type="audio/mpeg"
        )
    except Exception as e:
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