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
from mutagen.id3 import TIT2, TPE1, TALB, APIC, USLT

# Pastas universais
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_MUSICAS = os.path.join(BASE_DIR, "Musicas")
ARQ_HISTORICO = os.path.join(BASE_DIR, "historico.json")
QUALIDADE_MP3 = "320"

os.makedirs(PASTA_MUSICAS, exist_ok=True)

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
    # Remove conteúdos entre parênteses/colchetes
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", texto)
    # Remove termos comuns que atrapalham busca de letras
    t = re.sub(r"(?i)\b(prod\.|prod|feat\.|feat|ft\.|ft|official|audio|video|lyric|lyrics|sped up|slowed|reverb)\b", " ", t)
    # Remove pontuações e caracteres estranhos
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
# MOTOR DE BUSCA DE LETRAS MULTI-FALLBACK (LRCLIB + LyricsOVH + Vagalume)
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
    """Executa múltiplos fallbacks em paralelo com variações de nomes"""
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

HTML_PAGE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>SoundCloud Downloader Pro</title>
  
  <link rel="manifest" href="/manifest.json">
  <meta name="theme-color" content="#f97316">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="SoundCloud MP3">

  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    body {
      font-family: 'Plus Jakarta Sans', sans-serif;
      -webkit-tap-highlight-color: transparent;
    }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #090d16; }
    ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #334155; }
  </style>
</head>
<body class="bg-[#0b0f19] text-slate-100 min-h-screen flex flex-col items-center justify-start py-4 sm:py-10 px-3 sm:px-6 selection:bg-orange-500 selection:text-white">

  <!-- Container Principal -->
  <div class="w-full max-w-2xl bg-[#111726]/90 backdrop-blur-md border border-slate-800/80 rounded-2xl sm:rounded-3xl p-4 sm:p-7 shadow-2xl shadow-black/50">
    
    <!-- Topo / Cabeçalho -->
    <header class="flex items-center justify-between border-b border-slate-800/80 pb-4 mb-5">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 sm:w-12 sm:h-12 bg-gradient-to-tr from-orange-600 to-amber-500 text-white rounded-xl sm:rounded-2xl flex items-center justify-center font-black text-xl sm:text-2xl shadow-lg shadow-orange-500/20 shrink-0">
          ☁️
        </div>
        <div>
          <h1 class="text-base sm:text-lg font-bold text-white tracking-tight flex items-center gap-2">
            SoundCloud Downloader
            <span class="text-[10px] font-extrabold uppercase bg-orange-500/10 text-orange-400 px-2 py-0.5 rounded-full border border-orange-500/20">320kbps</span>
          </h1>
          <p class="text-[11px] sm:text-xs text-slate-400 font-medium">Letras Multi-Fonte • Capas Oficiais HD • MP3</p>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <button id="btnInstalarApp" class="hidden h-9 px-3 bg-orange-500/15 hover:bg-orange-500/25 active:scale-95 text-orange-400 text-xs font-semibold rounded-xl border border-orange-500/30 transition-all flex items-center gap-1.5 cursor-pointer">
          <span>📲</span> Instalar App
        </button>
        <button onclick="carregarHistorico()" class="h-9 px-3 bg-slate-800/80 hover:bg-slate-700 active:scale-95 text-slate-300 hover:text-white text-xs font-semibold rounded-xl border border-slate-700/80 transition-all flex items-center gap-1.5 shadow-sm cursor-pointer shrink-0">
          <span>🕒</span>
          <span class="hidden xs:inline sm:inline">Histórico</span>
        </button>
      </div>
    </header>

    <!-- Player de Áudio Flutuante -->
    <div id="playerBar" class="hidden mb-5 bg-[#080c14] border border-orange-500/30 rounded-2xl p-3.5 shadow-xl transition-all duration-300">
      <div class="flex items-center justify-between gap-3 mb-2.5">
        <div class="flex items-center gap-2.5 min-w-0">
          <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse shrink-0"></span>
          <div class="min-w-0">
            <p id="playerTitulo" class="text-xs font-bold text-white truncate"></p>
            <p class="text-[10px] text-orange-400 font-medium">Ouvindo prévia da faixa</p>
          </div>
        </div>
        <button onclick="fecharPlayer()" class="text-slate-400 hover:text-white text-xs px-2 py-1 bg-slate-800/80 rounded-lg transition">&times; Fechar</button>
      </div>
      <audio id="audioElement" controls class="w-full h-8 rounded-lg accent-orange-500"></audio>
    </div>

    <!-- Barra de Progresso Real de Download -->
    <div id="progressoDownloadBox" class="hidden mb-5 bg-[#080c14] border border-emerald-500/40 rounded-2xl p-4 shadow-xl">
      <div class="flex items-center justify-between text-xs mb-2">
        <span id="progressoTitulo" class="font-bold text-white truncate max-w-[70%]">Baixando música...</span>
        <span id="progressoPct" class="font-bold text-emerald-400">0%</span>
      </div>
      <div class="w-full bg-slate-800 rounded-full h-2.5 overflow-hidden mb-1.5">
        <div id="progressoBarra" class="bg-gradient-to-r from-emerald-500 to-teal-400 h-2.5 rounded-full transition-all duration-200" style="width: 0%"></div>
      </div>
      <div class="flex items-center justify-between text-[10px] text-slate-400">
        <span id="progressoStatus">Buscando letras e stream...</span>
        <span id="progressoDetalhe" class="text-emerald-400 font-mono">Multi-API Lyrics Active</span>
      </div>
    </div>

    <!-- Campo de Busca -->
    <div class="space-y-2 mb-5">
      <label class="block text-xs font-semibold text-slate-300 tracking-wide">Busque por nome da música ou cole o link:</label>
      <div class="flex flex-col sm:flex-row gap-2">
        <div class="relative flex-1">
          <input 
            type="text" 
            id="queryInput" 
            placeholder="Ex: kiss me again ou https://soundcloud.com/..." 
            onkeydown="if(event.key==='Enter') pesquisar()"
            class="w-full bg-[#080c14] border border-slate-700/80 rounded-xl px-4 py-3 text-sm text-white placeholder:text-slate-500 focus:outline-none focus:border-orange-500 focus:ring-1 focus:ring-orange-500 transition-all" 
          />
        </div>
        <button 
          id="btnBuscar" 
          onclick="pesquisar()" 
          class="h-11 sm:h-auto bg-gradient-to-r from-orange-500 to-amber-500 hover:from-orange-600 hover:to-amber-600 active:scale-[0.98] text-white font-bold text-xs sm:text-sm px-6 py-3 rounded-xl transition shadow-lg shadow-orange-500/20 flex items-center justify-center gap-2 cursor-pointer shrink-0"
        >
          <span>🔍</span> Buscar
        </button>
      </div>
      <div id="statusMsg" class="hidden text-xs font-semibold py-2 text-center rounded-xl"></div>
    </div>

    <!-- Lista de Resultados -->
    <div id="resultadosContainer" class="space-y-2.5">
      <div id="emptyState" class="py-12 text-center text-slate-500">
        <div class="text-3xl mb-2">🎧</div>
        <p class="text-xs font-medium">Digite o nome de uma música ou link acima para começar</p>
      </div>
    </div>

    <!-- Modal 1: Comparação de Capa (iTunes Encontrado) -->
    <div id="modalCapa" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div class="bg-[#111726] border border-slate-700 rounded-3xl max-w-sm sm:max-w-md w-full p-5 sm:p-6 shadow-2xl text-center animate-in fade-in zoom-in duration-200">
        <div class="w-12 h-12 bg-blue-500/15 text-blue-400 rounded-2xl flex items-center justify-center mx-auto mb-3 text-2xl border border-blue-500/20">🎨</div>
        <h3 class="font-bold text-sm sm:text-base text-white">Deseja trocar a capa da música?</h3>
        <p class="text-xs text-slate-400 mt-1 mb-4">Encontramos a capa oficial no iTunes em alta definição (600x600):</p>
        
        <div class="grid grid-cols-2 gap-3 mb-4">
          <div class="bg-[#080c14] border border-slate-800 p-2.5 rounded-2xl flex flex-col items-center">
            <span class="text-[9px] sm:text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2">Original SoundCloud</span>
            <img id="imgCapaOriginal" src="" class="w-24 h-24 sm:w-28 sm:h-28 object-cover rounded-xl border border-slate-800 shadow-md">
          </div>
          <div class="bg-[#080c14] border border-emerald-500/40 p-2.5 rounded-2xl flex flex-col items-center">
            <span class="text-[9px] sm:text-[10px] font-bold text-emerald-400 uppercase tracking-wider mb-2">Oficial iTunes HD</span>
            <img id="imgCapaItunes" src="" class="w-24 h-24 sm:w-28 sm:h-28 object-cover rounded-xl border border-emerald-500/30 shadow-md">
          </div>
        </div>

        <p id="txtDetalhesItunes" class="text-[11px] text-slate-300 mb-5 line-clamp-1 italic bg-[#080c14] py-2 px-3 rounded-xl border border-slate-800"></p>

        <div class="grid grid-cols-2 gap-2.5">
          <button id="btnUsarOriginal" class="h-11 bg-slate-800 hover:bg-slate-700 active:scale-95 text-slate-200 text-xs font-bold rounded-xl transition cursor-pointer">
            ❌ Manter Original
          </button>
          <button id="btnUsarItunes" class="h-11 bg-emerald-600 hover:bg-emerald-700 active:scale-95 text-white text-xs font-bold rounded-xl transition shadow-lg shadow-emerald-600/20 cursor-pointer">
            ✅ Trocar p/ iTunes
          </button>
        </div>
      </div>
    </div>

    <!-- Modal 2: Aviso quando NÃO acha no iTunes -->
    <div id="modalSemItunes" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div class="bg-[#111726] border border-amber-500/30 rounded-3xl max-w-sm sm:max-w-md w-full p-5 sm:p-6 shadow-2xl text-center animate-in fade-in zoom-in duration-200">
        <div class="w-12 h-12 bg-amber-500/15 text-amber-400 rounded-2xl flex items-center justify-center mx-auto mb-3 text-2xl border border-amber-500/20">⚠️</div>
        <h3 class="font-bold text-sm sm:text-base text-white">Capa não encontrada no iTunes</h3>
        <p class="text-xs text-slate-400 mt-1 mb-4">O iTunes não possui registro oficial para esta versão/remix.</p>
        
        <div class="bg-[#080c14] border border-slate-800 p-3 rounded-2xl flex items-center gap-3.5 mb-4 text-left">
          <img id="imgSemItunesCapa" src="" class="w-14 h-14 sm:w-16 sm:h-16 object-cover rounded-xl border border-slate-800 shrink-0">
          <div class="min-w-0 flex-1">
            <span class="text-[9px] font-bold text-orange-400 uppercase tracking-wider">Capa do SoundCloud</span>
            <p id="txtSemItunesTitulo" class="text-xs font-bold text-white truncate mt-0.5"></p>
            <p class="text-[11px] text-slate-400">A música será embutida com esta capa.</p>
          </div>
        </div>

        <p class="text-xs text-slate-300 font-semibold mb-5">Deseja prosseguir com o download mesmo assim?</p>

        <div class="grid grid-cols-2 gap-2.5">
          <button id="btnCancelarDownload" class="h-11 bg-slate-800 hover:bg-slate-700 active:scale-95 text-slate-300 text-xs font-bold rounded-xl transition cursor-pointer">
            ❌ Cancelar
          </button>
          <button id="btnConfirmarDownloadOriginal" class="h-11 bg-emerald-600 hover:bg-emerald-700 active:scale-95 text-white text-xs font-bold rounded-xl transition shadow-lg shadow-emerald-600/20 cursor-pointer">
            ✅ Sim, Baixar
          </button>
        </div>
      </div>
    </div>

    <!-- Modal de Histórico -->
    <div id="modalHistorico" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div class="bg-[#111726] border border-slate-800 rounded-3xl max-w-lg w-full p-5 sm:p-6 max-h-[80vh] flex flex-col shadow-2xl">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3 mb-3">
          <h3 class="font-bold text-sm text-white flex items-center gap-2"><span>🕒</span> Histórico de Downloads</h3>
          <button onclick="document.getElementById('modalHistorico').classList.add('hidden')" class="w-8 h-8 rounded-lg bg-slate-800 text-slate-400 hover:text-white flex items-center justify-center text-lg cursor-pointer">&times;</button>
        </div>
        <div id="listaHistorico" class="overflow-y-auto space-y-2 flex-1 divide-y divide-slate-800/60 text-xs pr-1"></div>
      </div>
    </div>

  </div>

  <footer class="mt-6 text-center text-slate-500 text-xs font-medium">
    SoundCloud Downloader • Processamento Seguro na Nuvem
  </footer>

  <script>
    const audioElement = document.getElementById('audioElement');
    const playerBar = document.getElementById('playerBar');
    const playerTitulo = document.getElementById('playerTitulo');

    let deferredPrompt;
    window.addEventListener('beforeinstallprompt', (e) => {
      e.preventDefault();
      deferredPrompt = e;
      const btn = document.getElementById('btnInstalarApp');
      if (btn) btn.classList.remove('hidden');
    });

    document.getElementById('btnInstalarApp').addEventListener('click', async () => {
      if (deferredPrompt) {
        deferredPrompt.prompt();
        const { outcome } = await deferredPrompt.userChoice;
        if (outcome === 'accepted') {
          document.getElementById('btnInstalarApp').classList.add('hidden');
        }
        deferredPrompt = null;
      }
    });

    function fecharPlayer() {
      audioElement.pause();
      audioElement.src = "";
      playerBar.classList.add('hidden');
    }

    async function pesquisar() {
      const query = document.getElementById('queryInput').value.trim();
      const status = document.getElementById('statusMsg');
      const btn = document.getElementById('btnBuscar');
      const container = document.getElementById('resultadosContainer');
      const empty = document.getElementById('emptyState');
      
      if (!query) {
        status.className = "text-center text-xs font-semibold text-amber-400 bg-amber-500/10 py-2.5 rounded-xl block border border-amber-500/20";
        status.innerText = "Por favor, digite o nome de uma música ou cole o link.";
        return;
      }
      
      if (empty) empty.classList.add('hidden');
      btn.disabled = true;
      btn.innerHTML = "<span>⏳</span> Buscando...";
      status.className = "text-center text-xs font-semibold text-orange-400 bg-orange-500/10 py-2.5 rounded-xl block border border-orange-500/20 animate-pulse";
      status.innerText = "Pesquisando faixas e carregando capas em HD...";
      container.innerHTML = "";

      const formData = new FormData();
      formData.append('query', query);
      try {
        const response = await fetch('/api/buscar', { method: 'POST', body: formData });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Erro na busca.");
        
        if (data.resultados.length === 0) {
          status.className = "text-center text-xs font-semibold text-slate-400 bg-slate-800/40 py-2.5 rounded-xl block border border-slate-700/60";
          status.innerText = "Nenhum resultado encontrado para esta busca.";
          return;
        }
        
        status.classList.add('hidden');
        data.resultados.forEach(item => {
          const div = document.createElement('div');
          const isCurto = item.segundos && item.segundos <= 45;

          div.className = `flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-[#080c14] border ${isCurto ? 'border-rose-900/60' : 'border-slate-800/90'} p-3.5 rounded-2xl hover:border-slate-700/90 transition-all duration-200 shadow-sm`;
          div.innerHTML = `
            <div class="flex items-center gap-3.5 min-w-0 flex-1">
              ${item.thumb ? `<img src="${item.thumb}" class="w-14 h-14 rounded-xl object-cover border border-slate-800 shrink-0 shadow-md" alt="Capa">` : `<div class="w-14 h-14 bg-slate-800 rounded-xl flex items-center justify-center text-xl shrink-0 shadow-md">🎵</div>`}
              <div class="min-w-0 flex-1">
                <h4 class="text-xs sm:text-sm font-bold text-white truncate tracking-tight">${item.titulo}</h4>
                <p class="text-[11px] text-slate-400 truncate mt-0.5 font-medium">${item.artista}</p>
                
                <div class="flex items-center gap-2 mt-1.5 flex-wrap">
                  ${item.duracao ? `
                    <span class="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-lg bg-slate-800/90 text-slate-300 border border-slate-700/60">
                      ⏱ ${item.duracao}
                    </span>
                  ` : ''}
                  ${isCurto ? `
                    <span class="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-lg bg-rose-950/80 text-rose-300 border border-rose-800/80">
                      ⚠️ Trecho curto (${item.duracao})
                    </span>
                  ` : ''}
                </div>
              </div>
            </div>
            
            <div class="flex items-center gap-2 pt-2 sm:pt-0 border-t sm:border-t-0 border-slate-800/60 justify-end shrink-0">
              <button onclick="ouvirPrevia('${item.url}', '${item.titulo.replace(/'/g, "\\'")}')" class="flex-1 sm:flex-initial h-10 px-3.5 bg-slate-800/90 hover:bg-slate-700 active:scale-95 text-slate-200 text-xs font-semibold rounded-xl transition flex items-center justify-center gap-1.5 cursor-pointer border border-slate-700/60">
                ▶ Ouvir
              </button>
              <button onclick="prepararDownload('${item.url}', '${item.titulo.replace(/'/g, "\\'")}', '${item.artista.replace(/'/g, "\\'")}', '${item.thumb || ''}', this)" class="flex-1 sm:flex-initial h-10 px-4 bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 active:scale-95 text-white text-xs font-bold rounded-xl transition flex items-center justify-center gap-1.5 shadow-md shadow-emerald-900/30 cursor-pointer">
                ⬇ Baixar MP3
              </button>
            </div>
          `;
          container.appendChild(div);
        });
      } catch (err) {
        status.className = "text-center text-xs font-semibold text-rose-400 bg-rose-500/10 py-2.5 rounded-xl block border border-rose-500/20";
        status.innerText = err.message;
      } finally {
        btn.disabled = false;
        btn.innerHTML = "<span>🔍</span> Buscar";
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

    async function prepararDownload(url, titulo, artista, thumbOriginal, btn) {
      const originalText = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = "<span>🎨</span> iTunes...";

      const formData = new FormData();
      formData.append('titulo', titulo);
      formData.append('artista', artista);

      try {
        const res = await fetch('/api/consultar-capa', { method: 'POST', body: formData });
        const data = await res.json();

        if (data.itunes) {
          abrirModalCapa(url, titulo, thumbOriginal, data.itunes.capa, data.itunes.detalhes, btn, originalText);
        } else {
          abrirModalSemItunes(url, titulo, thumbOriginal, btn, originalText);
        }
      } catch (err) {
        abrirModalSemItunes(url, titulo, thumbOriginal, btn, originalText);
      }
    }

    function abrirModalCapa(url, titulo, capaOriginal, capaItunes, detalhes, btn, originalBtnText) {
      const modal = document.getElementById('modalCapa');
      document.getElementById('imgCapaOriginal').src = capaOriginal || "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=150&auto=format&fit=crop&q=80";
      document.getElementById('imgCapaItunes').src = capaItunes;
      document.getElementById('txtDetalhesItunes').innerText = detalhes;

      document.getElementById('btnUsarOriginal').onclick = () => {
        modal.classList.add('hidden');
        executarDownload(url, titulo, capaOriginal, btn, originalBtnText);
      };

      document.getElementById('btnUsarItunes').onclick = () => {
        modal.classList.add('hidden');
        executarDownload(url, titulo, capaItunes, btn, originalBtnText);
      };

      modal.classList.remove('hidden');
    }

    function abrirModalSemItunes(url, titulo, capaOriginal, btn, originalBtnText) {
      const modal = document.getElementById('modalSemItunes');
      document.getElementById('imgSemItunesCapa').src = capaOriginal || "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=150&auto=format&fit=crop&q=80";
      document.getElementById('txtSemItunesTitulo').innerText = titulo;

      document.getElementById('btnCancelarDownload').onclick = () => {
        modal.classList.add('hidden');
        btn.innerHTML = originalBtnText;
        btn.disabled = false;
      };

      document.getElementById('btnConfirmarDownloadOriginal').onclick = () => {
        modal.classList.add('hidden');
        executarDownload(url, titulo, capaOriginal, btn, originalBtnText);
      };

      modal.classList.remove('hidden');
    }

    async function executarDownload(url, tituloOriginal, capaFinal, btn, originalText) {
      btn.disabled = true;
      btn.innerHTML = "<span>⏳</span> Baixando...";

      const pBox = document.getElementById('progressoDownloadBox');
      const pTitulo = document.getElementById('progressoTitulo');
      const pPct = document.getElementById('progressoPct');
      const pBarra = document.getElementById('progressoBarra');
      const pStatus = document.getElementById('progressoStatus');
      
      pBox.classList.remove('hidden');
      pTitulo.innerText = tituloOriginal;
      pPct.innerText = "0%";
      pBarra.style.width = "0%";
      pStatus.innerText = "Consultando bancos de letras e stream...";

      const downloadId = "dl_" + Date.now();

      const timerProgresso = setInterval(async () => {
        try {
          const r = await fetch('/api/progresso/' + downloadId);
          const pData = await r.json();
          if (pData && pData.pct !== undefined) {
            pPct.innerText = pData.pct + "%";
            pBarra.style.width = pData.pct + "%";
            if (pData.status) pStatus.innerText = pData.status;
          }
        } catch (_) {}
      }, 500);

      const formData = new FormData();
      formData.append('url', url);
      formData.append('download_id', downloadId);
      if (capaFinal) {
        formData.append('capa_custom', capaFinal);
      }

      try {
        const response = await fetch('/api/download', { method: 'POST', body: formData });
        clearInterval(timerProgresso);

        if (!response.ok) {
          const data = await response.json();
          throw new Error(data.detail || "Erro ao baixar.");
        }

        pPct.innerText = "100%";
        pBarra.style.width = "100%";
        pStatus.innerText = "✓ MP3 320kbps pronto com Letras + Capa!";

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

        btn.innerHTML = "<span>✓</span> Concluído!";
        setTimeout(() => { 
          btn.innerHTML = originalText; 
          btn.disabled = false;
          pBox.classList.add('hidden');
        }, 3500);
      } catch (err) {
        clearInterval(timerProgresso);
        alert("Erro no download: " + err.message);
        btn.innerHTML = originalText;
        btn.disabled = false;
        pBox.classList.add('hidden');
      }
    }

    async function carregarHistorico() {
      const modal = document.getElementById('modalHistorico');
      const lista = document.getElementById('listaHistorico');
      modal.classList.remove('hidden');
      lista.innerHTML = "<p class='text-slate-400 py-6 text-center text-xs'>Carregando registros...</p>";
      try {
        const res = await fetch('/api/historico');
        const data = await res.json();
        if (!data.historico || data.historico.length === 0) {
          lista.innerHTML = "<p class='text-slate-400 py-6 text-center text-xs'>Nenhum download registrado ainda.</p>";
          return;
        }
        lista.innerHTML = "";
        data.historico.reverse().forEach(h => {
          const item = document.createElement('div');
          item.className = "py-2.5 flex items-center justify-between gap-3";
          item.innerHTML = `
            <div class="min-w-0 flex-1">
              <p class="font-bold text-white truncate text-xs">${h.titulo}</p>
              <p class="text-slate-400 text-[10px] truncate">${h.artista}</p>
            </div>
            <span class="text-[10px] text-slate-500 font-mono shrink-0">${h.data}</span>
          `;
          lista.appendChild(item);
        });
      } catch (err) {
        lista.innerHTML = "<p class='text-rose-400 py-6 text-center text-xs'>Erro ao carregar histórico.</p>";
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

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML_PAGE)

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

        # 1. Tags básicas ID3
        corrigir_tags(arquivo_final)
        
        # 2. Embutir Capa
        if capa_custom and capa_custom.startswith("http"):
            embutir_capa_url(arquivo_final, capa_custom)
        elif thumb:
            embutir_capa_url(arquivo_final, thumb)

        # 3. Buscar e embutir Letras (LRCLIB + Lyrics.ovh + Vagalume em paralelo)
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
