import os
import re
import glob
import json
import urllib.request
import urllib.parse
import concurrent.futures
from datetime import datetime
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1, TALB, APIC

# Pastas universais
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

def obter_og_image(url):
    """Extrai a capa real do SoundCloud via meta tag og:image"""
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
        with urllib.request.urlopen(req, timeout=8) as r:
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

HTML_PAGE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SoundCloud Downloader Pro 320kbps</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style> body { font-family: 'Plus Jakarta Sans', sans-serif; } </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex flex-col items-center py-6 px-4">
  <div class="w-full max-w-2xl bg-slate-900 border border-slate-800/80 rounded-2xl p-5 sm:p-7 shadow-2xl">
    
    <!-- Cabeçalho -->
    <div class="flex items-center justify-between border-b border-slate-800 pb-4 mb-5">
      <div class="flex items-center gap-3">
        <div class="w-11 h-11 bg-orange-500/20 text-orange-400 rounded-xl flex items-center justify-center font-bold text-2xl border border-orange-500/30">☁️</div>
        <div>
          <h1 class="text-lg font-bold text-white tracking-tight">SoundCloud Downloader</h1>
          <p class="text-[11px] text-slate-400 font-medium">MP3 320kbps • Player de Prévia • Capas HD</p>
        </div>
      </div>
      <button onclick="carregarHistorico()" class="text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 px-3 py-1.5 rounded-lg border border-slate-700 transition cursor-pointer">🕒 Histórico</button>
    </div>

    <!-- Barra de Player Ativo (Global) -->
    <div id="playerBar" class="hidden mb-5 bg-slate-950 border border-orange-500/40 rounded-xl p-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-3 animate-in fade-in">
      <div class="flex items-center gap-2.5 min-w-0">
        <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse shrink-0"></span>
        <div class="min-w-0">
          <p id="playerTitulo" class="text-xs font-bold text-white truncate"></p>
          <p class="text-[10px] text-orange-400">Reproduzindo prévia da música</p>
        </div>
      </div>
      <audio id="audioElement" controls class="h-8 max-w-xs w-full"></audio>
    </div>

    <!-- Barra de Busca -->
    <div class="space-y-2 mb-5">
      <label class="block text-xs font-semibold text-slate-300">Digite o nome da música ou cole o link do SoundCloud:</label>
      <div class="flex gap-2">
        <input type="text" id="queryInput" placeholder="Ex: Misery ou https://soundcloud.com/..." onkeydown="if(event.key==='Enter') pesquisar()" class="flex-1 bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-orange-500 transition-colors" />
        <button id="btnBuscar" onclick="pesquisar()" class="bg-orange-500 hover:bg-orange-600 text-white font-bold text-xs px-5 py-3 rounded-xl transition shadow-lg shadow-orange-500/20 flex items-center justify-center cursor-pointer shrink-0">🔍 Buscar</button>
      </div>
      <div id="statusMsg" class="hidden text-xs font-semibold py-1.5 text-center"></div>
    </div>

    <!-- Lista de Resultados -->
    <div id="resultadosContainer" class="space-y-2.5"></div>

    <!-- Modal de Histórico -->
    <div id="modalHistorico" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-lg w-full p-5 max-h-[80vh] flex flex-col">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3 mb-3">
          <h3 class="font-bold text-sm text-white">Histórico de Downloads</h3>
          <button onclick="document.getElementById('modalHistorico').classList.add('hidden')" class="text-slate-400 hover:text-white text-lg cursor-pointer">&times;</button>
        </div>
        <div id="listaHistorico" class="overflow-y-auto space-y-2 flex-1 divide-y divide-slate-800 text-xs"></div>
      </div>
    </div>
  </div>

  <script>
    const audioElement = document.getElementById('audioElement');
    const playerBar = document.getElementById('playerBar');
    const playerTitulo = document.getElementById('playerTitulo');

    async function pesquisar() {
      const query = document.getElementById('queryInput').value.trim();
      const status = document.getElementById('statusMsg');
      const btn = document.getElementById('btnBuscar');
      const container = document.getElementById('resultadosContainer');
      if (!query) { alert("Por favor, digite o nome ou link."); return; }
      
      btn.disabled = true;
      btn.innerText = "Buscando...";
      status.className = "text-center text-xs font-semibold text-orange-400 py-2 block";
      status.innerText = "Pesquisando faixas e carregando capas em HD...";
      container.innerHTML = "";

      const formData = new FormData();
      formData.append('query', query);
      try {
        const response = await fetch('/api/buscar', { method: 'POST', body: formData });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Erro na busca.");
        if (data.resultados.length === 0) {
          status.className = "text-center text-xs font-semibold text-slate-400 py-2 block";
          status.innerText = "Nenhum resultado encontrado.";
          return;
        }
        status.classList.add('hidden');
        data.resultados.forEach(item => {
          const div = document.createElement('div');
          div.className = "flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-slate-950 border border-slate-800/80 p-3.5 rounded-xl hover:border-slate-700 transition";
          div.innerHTML = `
            <div class="flex items-center gap-3 min-w-0 flex-1">
              ${item.thumb ? `<img src="${item.thumb}" class="w-14 h-14 rounded-lg object-cover border border-slate-800 shrink-0 shadow-sm" alt="Capa">` : `<div class="w-14 h-14 bg-slate-800 rounded-lg flex items-center justify-center text-lg shrink-0">🎵</div>`}
              <div class="min-w-0 flex-1">
                <h4 class="text-xs font-bold text-white truncate">${item.titulo}</h4>
                <p class="text-[11px] text-slate-400 truncate mt-0.5">${item.artista} ${item.duracao ? `• ${item.duracao}` : ''}</p>
              </div>
            </div>
            <div class="flex items-center gap-2 self-end sm:self-center shrink-0">
              <button onclick="ouvirPrevia('${item.url}', '${item.titulo.replace(/'/g, "\\'")}')" class="bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold px-3 py-2 rounded-lg transition flex items-center gap-1 cursor-pointer">
                ▶ Ouvir
              </button>
              <button onclick="baixarMusica('${item.url}', '${item.titulo.replace(/'/g, "\\'")}', this)" class="bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold px-3.5 py-2 rounded-lg transition flex items-center gap-1.5 shadow-sm shadow-emerald-600/20 cursor-pointer">
                ⬇ Baixar MP3
              </button>
            </div>
          `;
          container.appendChild(div);
        });
      } catch (err) {
        status.className = "text-center text-xs font-semibold text-rose-500 py-2 block";
        status.innerText = err.message;
      } finally {
        btn.disabled = false;
        btn.innerText = "🔍 Buscar";
      }
    }

    async function ouvirPrevia(url, titulo) {
      playerBar.classList.remove('hidden');
      playerTitulo.innerText = "Carregando áudio: " + titulo;
      audioElement.src = "";
      
      const formData = new FormData();
      formData.append('url', url);

      try {
        const response = await fetch('/api/stream', { method: 'POST', body: formData });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Não foi possível carregar o áudio.");

        audioElement.src = data.stream_url;
        playerTitulo.innerText = titulo;
        audioElement.play();
      } catch (err) {
        alert("Erro ao tocar prévia: " + err.message);
        playerBar.classList.add('hidden');
      }
    }

    async function baixarMusica(url, tituloOriginal, btn) {
      const originalText = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = "⏳ Baixando...";
      const formData = new FormData();
      formData.append('url', url);

      try {
        const response = await fetch('/api/download', { method: 'POST', body: formData });
        if (!response.ok) {
          const data = await response.json();
          throw new Error(data.detail || "Erro ao baixar.");
        }

        const disposition = response.headers.get('Content-Disposition');
        let filename = tituloOriginal ? `${tituloOriginal}.mp3` : "musica.mp3";
        if (disposition && disposition.indexOf('filename=') !== -1) {
          const matches = disposition.match(/filename="?([^";]+)"?/);
          if (matches && matches[1]) {
            filename = decodeURIComponent(matches[1]);
          }
        }

        const blob = await response.blob();
        const downloadUrl = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = downloadUrl;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(downloadUrl);

        btn.innerHTML = "✓ Concluído!";
        setTimeout(() => { btn.innerHTML = originalText; btn.disabled = false; }, 3000);
      } catch (err) {
        alert("Erro no download: " + err.message);
        btn.innerHTML = originalText;
        btn.disabled = false;
      }
    }

    async function carregarHistorico() {
      const modal = document.getElementById('modalHistorico');
      const lista = document.getElementById('listaHistorico');
      modal.classList.remove('hidden');
      lista.innerHTML = "<p class='text-slate-400 py-4 text-center'>Carregando...</p>";
      try {
        const res = await fetch('/api/historico');
        const data = await res.json();
        if (!data.historico || data.historico.length === 0) {
          lista.innerHTML = "<p class='text-slate-400 py-4 text-center'>Nenhum download ainda.</p>";
          return;
        }
        lista.innerHTML = "";
        data.historico.reverse().forEach(h => {
          const item = document.createElement('div');
          item.className = "py-2";
          item.innerHTML = `<p class="font-bold text-white">${h.titulo}</p><p class="text-slate-400 text-[11px]">${h.artista} • ${h.data}</p>`;
          lista.appendChild(item);
        });
      } catch (err) {
        lista.innerHTML = "<p class='text-rose-500 py-4 text-center'>Erro ao ler histórico.</p>";
      }
    }
  </script>
</body>
</html>
"""

app = FastAPI(title="SoundCloud MP3 Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML_PAGE)

@app.post("/api/buscar")
async def buscar_faixas(query: str = Form(...)):
    query = query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Digite uma busca válida.")
    
    # Se for link direto
    if query.startswith("http"):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(query, download=False)
                thumb = info.get("thumbnail") or obter_og_image(query)
                return {
                    "resultados": [{
                        "url": query,
                        "titulo": info.get("title", "Sem título"),
                        "artista": info.get("uploader", "Desconhecido"),
                        "duracao": info.get("duration_string", ""),
                        "thumb": thumb
                    }]
                }
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erro no link: {limpar_ansi(str(e))}")

    # Pesquisa de 10 faixas no SoundCloud com busca paralela de capas
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
                dur = ent.get("duration_string") or ""
                thumb = ent.get("thumbnail")
                
                raw_resultados.append({
                    "url": url,
                    "titulo": titulo,
                    "artista": artista,
                    "duracao": dur,
                    "thumb": thumb
                })
                
                if not thumb and url:
                    urls_sem_capa.append((idx, url))

            # Busca capas em paralelo com Threads para máxima velocidade
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
            thumb = info.get("thumbnail") or obter_og_image(url)

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
            media_type="audio/mpeg",
            headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(nome_limpo)}"'}
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
