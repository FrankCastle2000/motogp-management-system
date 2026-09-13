const TAB_AUTH_KEY = "motogp:tab-authenticated";

function isTabAuthenticated() {
  try {
    return sessionStorage.getItem(TAB_AUTH_KEY) === "true";
  } catch (err) {
    return false;
  }
}

function markTabAuthenticated() {
  try {
    sessionStorage.setItem(TAB_AUTH_KEY, "true");
  } catch (err) {
    // 无法使用会话存储时仍允许本次登录继续。
  }
}

function clearTabAuthentication() {
  try {
    sessionStorage.removeItem(TAB_AUTH_KEY);
  } catch (err) {
    // 忽略浏览器禁用会话存储的情况。
  }
}

const tabSessionReady = (async () => {
  if (isTabAuthenticated()) return;
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "include",
      keepalive: true,
    });
  } catch (err) {
    // 网络失败时，后续 /api/auth/me 仍会执行常规鉴权。
  }
})();

async function apiRequest(url, options = {}) {
  const res = await fetch(url, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    const err = new Error(data.message || "请求失败");
    err.status = res.status;
    err.retryAfter = data.retry_after;
    throw err;
  }
  return data;
}

async function getCurrentUser() {
  await tabSessionReady;
  try {
    const result = await apiRequest("/api/auth/me");
    return result.data;
  } catch (err) {
    return null;
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function requireLogin(redirectTo = "/login.html") {
  const user = await getCurrentUser();
  if (!user) {
    navigateSitePage(redirectTo);
    return null;
  }
  return user;
}

async function logout() {
  clearTabAuthentication();
  await apiRequest("/api/auth/logout", { method: "POST" });
  await navigateSitePage("/login.html");
}

let siteNavigationId = 0;

function isSoftNavigationUrl(url) {
  return url.origin === window.location.origin && /\/(login|register|riders|teams|calendar|standings|change-password|users|logs)\.html$/.test(url.pathname);
}

function hasLoadedScript(src) {
  const absoluteSrc = new URL(src, window.location.href).href;
  return Array.from(document.scripts).some((script) => script.src === absoluteSrc);
}

function loadPageScript(src) {
  if (hasLoadedScript(src)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`脚本加载失败：${src}`));
    document.head.appendChild(script);
  });
}

async function navigateSitePage(url, pushState = true) {
  const targetUrl = new URL(url, window.location.href);
  if (!isSoftNavigationUrl(targetUrl)) {
    window.location.href = targetUrl.href;
    return;
  }

  const navigationId = ++siteNavigationId;
  try {
    const response = await fetch(targetUrl.pathname + targetUrl.search, {
      credentials: "include",
      headers: { "X-Requested-With": "soft-navigation" },
    });
    if (!response.ok) throw new Error("页面加载失败");

    const html = await response.text();
    if (navigationId !== siteNavigationId) return;

    const nextDocument = new DOMParser().parseFromString(html, "text/html");
    const nextRoot = nextDocument.querySelector(".wrap, .auth-wrap");
    const currentRoot = document.querySelector(".wrap, .auth-wrap");
    if (!nextRoot || !currentRoot) throw new Error("页面内容无效");

    currentRoot.replaceWith(nextRoot);
    document.title = nextDocument.title;
    if (pushState) history.pushState({ softNavigation: true }, "", targetUrl.href);

    const scripts = Array.from(nextDocument.querySelectorAll("script"));
    for (const script of scripts) {
      const src = script.getAttribute("src");
      if (src) await loadPageScript(src);
    }

    for (const script of scripts) {
      if (!script.getAttribute("src") && script.textContent.trim()) {
        new Function(script.textContent)();
      }
    }

    if (nextRoot.classList.contains("auth-wrap") && typeof window.initAuthForm === "function") {
      window.initAuthForm();
    }
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  } catch (err) {
    window.location.href = targetUrl.href;
  }
}

document.addEventListener("click", (event) => {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
    return;
  }
  const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
  if (!link || link.target || link.hasAttribute("download")) return;

  const targetUrl = new URL(link.href, window.location.href);
  if (!isSoftNavigationUrl(targetUrl)) return;

  event.preventDefault();
  navigateSitePage(targetUrl.href);
});

window.addEventListener("popstate", () => {
  const targetUrl = new URL(window.location.href);
  if (isSoftNavigationUrl(targetUrl)) navigateSitePage(targetUrl.href, false);
});

const MUSIC_VOLUME_KEY = "motogp:bgm:volume";
const MUSIC_TIME_KEY = "motogp:bgm:current-time";

function readStorage(storage, key, fallback = null) {
  try {
    const value = storage.getItem(key);
    return value === null ? fallback : value;
  } catch (err) {
    return fallback;
  }
}

function writeStorage(storage, key, value) {
  try {
    storage.setItem(key, String(value));
  } catch (err) {
    // 浏览器禁用存储时仍允许正常播放。
  }
}

const SELECTED_SEASON_KEY = "motogp:selected-season";

function readSelectedSeason(fallback = new Date().getFullYear()) {
  const value = Number(readStorage(sessionStorage, SELECTED_SEASON_KEY, fallback));
  return Number.isInteger(value) && value >= 1949 && value <= 2100 ? value : fallback;
}

function rememberSelectedSeason(year) {
  const value = Number(year);
  if (Number.isInteger(value) && value >= 1949 && value <= 2100) {
    writeStorage(sessionStorage, SELECTED_SEASON_KEY, value);
  }
}

function initBackgroundMusic() {
  if (document.getElementById("music-player")) return;

  const audio = document.createElement("audio");
  audio.id = "background-music";
  audio.src = "/assets/audio/background-music.mp3";
  audio.loop = true;
  audio.preload = "metadata";
  audio.playsInline = true;

  const savedVolume = Number(readStorage(localStorage, MUSIC_VOLUME_KEY, "0.35"));
  audio.volume = Number.isFinite(savedVolume)
    ? Math.min(1, Math.max(0, savedVolume))
    : 0.35;

  let musicEnabled = true;
  audio.autoplay = musicEnabled;

  const player = document.createElement("div");
  player.className = "music-player";
  player.id = "music-player";

  const toggleButton = document.createElement("button");
  toggleButton.type = "button";
  toggleButton.className = "music-toggle";
  const musicIcon = document.createElement("img");
  musicIcon.src = "/assets/images/music-icon.png";
  musicIcon.alt = "";
  musicIcon.setAttribute("aria-hidden", "true");
  toggleButton.appendChild(musicIcon);

  const volumeControl = document.createElement("label");
  volumeControl.className = "music-volume";
  volumeControl.setAttribute("aria-label", "背景音乐音量");
  volumeControl.innerHTML = '<input type="range" min="0" max="1" step="0.05" aria-label="背景音乐音量" />';
  const volumeInput = volumeControl.querySelector("input");
  volumeInput.value = String(audio.volume);

  player.append(toggleButton, volumeControl, audio);
  document.body.appendChild(player);

  function updateMusicButton() {
    const playing = !audio.paused;
    toggleButton.classList.toggle("playing", playing);
    toggleButton.setAttribute("aria-pressed", String(playing));
    toggleButton.setAttribute("aria-label", playing ? "暂停背景音乐" : "播放背景音乐");
    toggleButton.title = playing ? "暂停背景音乐" : "播放背景音乐";
  }

  async function playMusic() {
    if (!musicEnabled) return false;
    try {
      await audio.play();
      updateMusicButton();
      return true;
    } catch (err) {
      updateMusicButton();
      player.classList.add("autoplay-blocked");
      return false;
    }
  }

  audio.addEventListener("loadedmetadata", () => {
    const savedTime = Number(readStorage(sessionStorage, MUSIC_TIME_KEY, "0"));
    if (Number.isFinite(savedTime) && savedTime > 0 && savedTime < audio.duration) {
      audio.currentTime = savedTime;
    }
  });

  audio.addEventListener("play", () => {
    player.classList.remove("autoplay-blocked");
    updateMusicButton();
  });
  audio.addEventListener("pause", updateMusicButton);

  toggleButton.addEventListener("click", async () => {
    if (audio.paused) {
      musicEnabled = true;
      await playMusic();
    } else {
      musicEnabled = false;
      audio.pause();
    }
  });

  volumeInput.addEventListener("input", () => {
    audio.volume = Number(volumeInput.value);
    writeStorage(localStorage, MUSIC_VOLUME_KEY, audio.volume);
  });

  window.addEventListener("pagehide", () => {
    if (Number.isFinite(audio.currentTime)) {
      writeStorage(sessionStorage, MUSIC_TIME_KEY, audio.currentTime);
    }
  });

  if (musicEnabled) {
    playMusic();
    const unlockMusic = (event) => {
      const insidePlayer = event.target instanceof Element && event.target.closest("#music-player");
      if (audio.paused && !insidePlayer) playMusic();
    };
    document.addEventListener("pointerdown", unlockMusic, { once: true, capture: true });
    document.addEventListener("keydown", unlockMusic, { once: true, capture: true });
  }

  updateMusicButton();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initBackgroundMusic);
} else {
  initBackgroundMusic();
}
