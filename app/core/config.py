import os
import json
from fastapi.templating import Jinja2Templates

# ================= 路径配置 =================
CONFIG_DIR = "/app/config"
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
FONT_DIR = os.path.join(CONFIG_DIR, "fonts")
if not os.path.exists(FONT_DIR):
    os.makedirs(FONT_DIR, exist_ok=True)

# ================= 资源常量 =================
FONT_URL = "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/Simplified/NotoSansCJKsc-Bold.otf"
FONT_PATH = os.path.join(FONT_DIR, "NotoSansCJKsc-Bold.otf")

# 固定日报封面
REPORT_COVER_URL = "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=1200&auto=format&fit=crop"
FALLBACK_IMAGE_URL = "https://img.hotimg.com/a444d32a033994d5b.png"

TMDB_FALLBACK_POOL = [
    "https://image.tmdb.org/t/p/original/zfbjgQE1uSd9wiPTX4VzsLi0rGG.jpg",
    "https://image.tmdb.org/t/p/original/rLb2cs785pePbIKYQz1CADtovh7.jpg",
    "https://image.tmdb.org/t/p/original/tmU7GeKVybMWFButWEGl2M4GeiP.jpg",
    "https://image.tmdb.org/t/p/original/kXfqcdQKsToO0OUXHcrrNCHDBzO.jpg",
    "https://image.tmdb.org/t/p/original/zb6fM1CX41D9rF9hdgclu0peUmy.jpg", 
    "https://image.tmdb.org/t/p/original/vI3aUGTuRRdM7J78KIdW98Lnidq.jpg",
    "https://image.tmdb.org/t/p/original/jXJxMcVoEuXzym3vFnjqDW4ifo6.jpg",
    "https://image.tmdb.org/t/p/original/sRLC052ieEroxViUFWa3KD77SII.jpg",
    "https://image.tmdb.org/t/p/original/mSDsSDwaP3E7dEfUPWy4J0djt4O.jpg",
    "https://image.tmdb.org/t/p/original/lzWHmYdfeFiMIY4JaMmtR7GEli3.jpg",
]

# 主题库
THEMES = {
    "black_gold": {"bg": (26, 26, 26), "text": (255, 255, 255), "card": (255, 255, 255, 20), "highlight": (234, 179, 8)},
    "cyber":      {"bg": (46, 16, 101), "text": (255, 255, 255), "card": (255, 255, 255, 20), "highlight": (0, 255, 255)},
    "ocean":      {"bg": (15, 23, 42),  "text": (255, 255, 255), "card": (255, 255, 255, 20), "highlight": (56, 189, 248)},
    "aurora":     {"bg": (6, 78, 59),   "text": (255, 255, 255), "card": (255, 255, 255, 20), "highlight": (52, 211, 153)},
    "magma":      {"bg": (127, 29, 29), "text": (255, 255, 255), "card": (255, 255, 255, 20), "highlight": (251, 146, 60)},
    "sunset":     {"bg": (124, 45, 18), "text": (255, 255, 255), "card": (255, 255, 255, 20), "highlight": (253, 186, 116)},
    "concrete":   {"bg": (82, 82, 82),  "text": (255, 255, 255), "card": (255, 255, 255, 20), "highlight": (212, 212, 216)},
    "white":      {"bg": (255, 255, 255), "text": (51, 51, 51), "card": (0, 0, 0, 10), "highlight": (234, 179, 8)}
}

# 默认配置字典
DEFAULT_CONFIG = {
    "emby_host": os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/'),
    "emby_api_key": os.getenv("EMBY_API_KEY", "").strip(),
    "tmdb_api_key": os.getenv("TMDB_API_KEY", "").strip(),
    "proxy_url": "",
    "hidden_users": [],
    "tg_bot_token": "",
    "tg_chat_id": "",     
    "enable_bot": False,  
    "enable_notify": False,
    "enable_library_notify": False,  # 🔥 新增：入库通知开关
    "scheduled_tasks": []
}

class ConfigManager:
    def __init__(self):
        self.config = DEFAULT_CONFIG.copy()
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                    self.config.update(saved)
            except Exception as e: 
                print(f"⚠️ Config Load Error: {e}")
    
    def save(self):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e: 
            print(f"⚠️ Config Save Error: {e}")

    def get(self, key): 
        return self.config.get(key, DEFAULT_CONFIG.get(key))
    
    def set(self, key, value): 
        self.config[key] = value
        self.save()
    
    def get_all(self): 
        return self.config

# ================= 全局单例与常量 =================
cfg = ConfigManager()
templates = Jinja2Templates(directory="templates")

SECRET_KEY = os.getenv("SECRET_KEY", "embypulse_secret_key_2026")
PORT = 10307
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")