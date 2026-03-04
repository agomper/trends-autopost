import os, json, time, requests, subprocess, random, shutil, pickle
from datetime import datetime
from google import genai
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from instagrapi import Client

# ============================================================
# Configuracion — todo viene de variables de entorno (GitHub Secrets)
# ============================================================
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
YOUTUBE_API_KEY  = os.environ["YOUTUBE_API_KEY"]
INSTAGRAM_USER   = os.environ["INSTAGRAM_USER"]
ELEVENLABS_VOICE = "Q0NjdvleZbRtgDhUJamI"

N_COMENTARIOS  = 8
DURACION_CLIP  = 5
OUTPUT_DIR     = "/tmp/comentarios/"
BLACKLIST_PATH = "blacklist.json"   # en el repo, GitHub Actions hace commit al final

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============================================================
# Blacklist
# ============================================================
def cargar_blacklist(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        print("Blacklist cargada: " + str(len(data)) + " videos")
        return set(data)
    print("Blacklist nueva (vacia)")
    return set()

def guardar_blacklist(path, blacklist):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(blacklist), f, ensure_ascii=False, indent=2)
    print("Blacklist guardada: " + str(len(blacklist)) + " videos")

# ============================================================
# YouTube — buscar videos
# ============================================================
CATEGORIAS_YT = ["0", "10", "17", "20", "23", "24", "25"]

def _fetch_categoria(api_key, categoria_id, max_results=50):
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part":            "snippet,statistics",
                "chart":           "mostPopular",
                "regionCode":      "ES",
                "maxResults":      max_results,
                "videoCategoryId": categoria_id,
                "key":             api_key,
            }
        )
        r.raise_for_status()
        videos = []
        for item in r.json().get("items", []):
            vid_id   = item["id"]
            titulo   = item["snippet"]["title"]
            canal    = item["snippet"]["channelTitle"]
            comments = int(item["statistics"].get("commentCount", 0))
            videos.append({
                "id": vid_id, "titulo": titulo, "canal": canal,
                "comments": comments,
                "url": "https://www.youtube.com/watch?v=" + vid_id,
                "categoria": categoria_id,
            })
        return videos
    except Exception as e:
        print("Categoria " + categoria_id + " fallida: " + str(e))
        return []

def buscar_videos_virales(api_key, blacklist, max_results=50):
    print("Buscando videos virales en Espana (" + str(len(CATEGORIAS_YT)) + " categorias)...")
    pool = {}
    for cat in CATEGORIAS_YT:
        videos_cat = _fetch_categoria(api_key, cat, max_results)
        nuevos = sum(1 for v in videos_cat if v["id"] not in pool)
        for v in videos_cat:
            if v["id"] not in pool:
                pool[v["id"]] = v
        print("  Cat " + cat + ": " + str(len(videos_cat)) + " videos, " + str(nuevos) + " nuevos")
        time.sleep(0.3)
    todos       = sorted(pool.values(), key=lambda x: x["comments"], reverse=True)
    disponibles = [v for v in todos if v["id"] not in blacklist]
    print("  TOTAL: " + str(len(todos)) + " | disponibles: " + str(len(disponibles)) + " | blacklist: " + str(len(todos) - len(disponibles)))
    if not disponibles:
        print("Todos usados — reseteando blacklist")
        guardar_blacklist(BLACKLIST_PATH, set())
        disponibles = list(todos)
    for v in disponibles[:3]:
        print("  * [cat" + v["categoria"] + "] " + v["titulo"][:45] + " — " + str(v["comments"]) + " comentarios")
    return disponibles

def obtener_comentarios(video_id, api_key, max_results=100):
    print("Obteniendo comentarios...")
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/commentThreads",
        params={
            "part":       "snippet",
            "videoId":    video_id,
            "maxResults": max_results,
            "order":      "relevance",
            "key":        api_key,
        }
    )
    r.raise_for_status()
    comentarios = []
    for item in r.json().get("items", []):
        c = item["snippet"]["topLevelComment"]["snippet"]
        comentarios.append({
            "texto": c["textDisplay"],
            "likes": c["likeCount"],
            "autor": c["authorDisplayName"],
        })
    comentarios.sort(key=lambda x: x["likes"], reverse=True)
    print("  " + str(len(comentarios)) + " comentarios")
    return comentarios

# ============================================================
# Gemini
# ============================================================
def generar_guion(video_info, comentarios, api_key, n=2):
    client = genai.Client(api_key=api_key)
    lista  = json.dumps(
        [{"texto": c["texto"], "likes": c["likes"], "autor": c["autor"]} for c in comentarios[:50]],
        ensure_ascii=False, indent=2
    )
    prompt = (
        "Eres el editor de un canal de humor negro en YouTube/TikTok espanol.\n"
        "Video: " + video_info["titulo"] + " — canal: " + video_info["canal"] + "\n\n"
        "Comentarios mas votados:\n" + lista + "\n\n"
        "Elige los " + str(n) + " comentarios MAS graciosos, oscuros o sarcasticos.\n"
        "IMPORTANTE: maximo 100 caracteres por comentario para que quepan en pantalla.\n"
        "Si el comentario original es mas largo, resumelo manteniendo la gracia.\n"
        "CRITICO: en el campo autor pon EXACTAMENTE el nombre real del campo autor que te llega.\n\n"
        "Responde SOLO JSON sin markdown:\n"
        "{\n"
        "  \"comentarios\": [\n"
        "    {\"num\": 1, \"comentario\": \"texto\", \"likes\": 123, \"autor\": \"nombre\"}\n"
        "  ],\n"
        "  \"caption\": \"caption viral espanol emojis humor oscuro 5 hashtags\"\n"
        "}"
    )
    for intento in range(1, 4):
        try:
            resp  = client.models.generate_content(model="gemini-2.5-flash-lite", contents=prompt)
            txt   = resp.text.replace("```json", "").replace("```", "").strip()
            datos = json.loads(txt[txt.find("{"):txt.rfind("}")+1])
            print("Guion OK — " + str(len(datos["comentarios"])) + " comentarios")
            return datos
        except Exception as e:
            print("Intento " + str(intento) + " fallido: " + str(e))
            if intento == 3: raise
            time.sleep(2)

# ============================================================
# Video
# ============================================================
def descargar_video(url, output_dir):
    print("Descargando video...")
    ts       = datetime.now().strftime("%H%M%S")
    out_path = output_dir + "video_original_" + ts + ".mp4"
    for f in os.listdir(output_dir):
        if f.startswith("video_original"):
            os.remove(output_dir + f)
    r = subprocess.run([
        "yt-dlp",
        "-f", "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
        "--merge-output-format", "mp4",
        "--force-overwrites",
        "--js-runtimes", "nodejs",
        "-o", out_path, "--no-playlist", url
    ], capture_output=True)
    if r.returncode != 0:
        raise Exception("yt-dlp: " + r.stderr.decode()[:300])
    print("  OK: " + out_path)
    return out_path

def get_duracion(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True
    )
    return float(r.stdout.strip())

def preprocesar_video(video_original, output_dir, n_comentarios, duracion_clip):
    print("Pre-procesando video...")
    video_proc    = output_dir + "video_proc.mp4"
    vid_dur_total = get_duracion(video_original)
    seg_total     = min(n_comentarios * duracion_clip * 4, vid_dur_total)
    r = subprocess.run([
        "ffmpeg", "-y",
        "-t", str(seg_total), "-i", video_original,
        "-vf", "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=720:1280,colorchannelmixer=rr=0.55:gg=0.55:bb=0.55",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-pix_fmt", "yuv420p",
        video_proc
    ], capture_output=True)
    if r.returncode != 0:
        raise Exception("preproceso: " + r.stderr.decode()[-300:])
    vid_proc_dur = get_duracion(video_proc)
    print("OK video procesado: " + str(round(vid_proc_dur, 1)) + "s")
    return video_proc, vid_proc_dur

# ============================================================
# Overlay
# ============================================================
def encontrar_fuente(bold=False):
    candidatos_bold = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    candidatos_reg = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for p in (candidatos_bold if bold else candidatos_reg):
        if os.path.exists(p):
            return p
    return None

def crear_overlay(texto, autor, likes, num, output_dir, ancho=720, alto=1280):
    from PIL import Image, ImageDraw, ImageFont

    FS_AUTOR = 20
    FS_TEXT  = 28
    FS_LIKES = 18
    PAD_X    = 18
    PAD_Y    = 14
    MAX_W    = ancho - PAD_X * 2 - 60
    AVATAR_R = 20
    BOX_X    = 20
    BOX_W    = ancho - 40

    ruta_bold = encontrar_fuente(bold=True)
    ruta_reg  = encontrar_fuente(bold=False)

    try:
        font_autor = ImageFont.truetype(ruta_reg,  FS_AUTOR) if ruta_reg  else ImageFont.load_default()
        font_text  = ImageFont.truetype(ruta_reg,  FS_TEXT)  if ruta_reg  else ImageFont.load_default()
        font_likes = ImageFont.truetype(ruta_reg,  FS_LIKES) if ruta_reg  else ImageFont.load_default()
    except Exception:
        font_autor = font_text = font_likes = ImageFont.load_default()

    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    palabras, lineas, linea = texto.split(), [], ""
    for p in palabras:
        prueba = (linea + " " + p).strip()
        if tmp.textlength(prueba, font=font_text) <= MAX_W:
            linea = prueba
        else:
            if linea: lineas.append(linea)
            linea = p
    if linea: lineas.append(linea)

    LINE_H  = FS_TEXT + 8
    AUTOR_H = FS_AUTOR + 6
    LIKES_H = FS_LIKES + 6
    box_h   = PAD_Y + AUTOR_H + 6 + len(lineas) * LINE_H + 10 + 1 + 8 + LIKES_H + PAD_Y
    box_y   = int(alto * 0.55)

    overlay = Image.new("RGBA", (ancho, alto), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    draw.rounded_rectangle([BOX_X, box_y, BOX_X+BOX_W, box_y+box_h], radius=12, fill=(40,40,40,175))
    draw.rounded_rectangle([BOX_X, box_y, BOX_X+BOX_W, box_y+box_h], radius=12, outline=(255,255,255,40), width=1)

    avatar_x = BOX_X + PAD_X
    avatar_y = box_y + PAD_Y
    hue = sum(ord(c) for c in autor) % 6
    colores_avatar = [(66,133,244),(234,67,53),(251,188,5),(52,168,83),(103,58,183),(0,172,193)]
    draw.ellipse([avatar_x, avatar_y, avatar_x+AVATAR_R*2, avatar_y+AVATAR_R*2], fill=colores_avatar[hue]+(230,))

    inicial = autor[0].upper() if autor else "?"
    try:
        font_av = ImageFont.truetype(ruta_bold, 18) if ruta_bold else ImageFont.load_default()
    except Exception:
        font_av = ImageFont.load_default()
    bbox = tmp.textbbox((0,0), inicial, font=font_av)
    lw, lh = bbox[2]-bbox[0], bbox[3]-bbox[1]
    draw.text((avatar_x+AVATAR_R-lw//2, avatar_y+AVATAR_R-lh//2-1), inicial, font=font_av, fill=(255,255,255,255))

    texto_x = avatar_x + AVATAR_R * 2 + 10
    draw.text((texto_x, avatar_y+2), autor, font=font_autor, fill=(200,200,200,255))

    y = box_y + PAD_Y + AUTOR_H + 6
    for l in lineas:
        draw.text((texto_x, y), l, font=font_text, fill=(255,255,255,255))
        y += LINE_H

    y += 6
    draw.line([(BOX_X+PAD_X, y), (BOX_X+BOX_W-PAD_X, y)], fill=(255,255,255,30), width=1)
    y += 8
    draw.text((texto_x, y), "👍  " + str(likes), font=font_likes, fill=(160,160,160,220))

    path = output_dir + "overlay_" + str(num).zfill(2) + ".png"
    overlay.save(path)
    return path

def montar_segmento(video_proc, texto, autor, likes, num, output_dir, duracion, offset):
    frag    = output_dir + "frag_"  + str(num).zfill(2) + ".mp4"
    out     = output_dir + "seg_"   + str(num).zfill(2) + ".mp4"
    overlay = crear_overlay(texto, autor, likes, num, output_dir)
    subprocess.run(["ffmpeg","-y","-ss",str(offset),"-i",video_proc,"-t",str(duracion),"-c","copy",frag], capture_output=True)
    r = subprocess.run([
        "ffmpeg","-y","-i",frag,"-i",overlay,
        "-filter_complex","[0:v][1:v]overlay=0:0[v]",
        "-map","[v]","-map","0:a",
        "-c:v","libx264","-c:a","aac","-pix_fmt","yuv420p","-t",str(duracion),out
    ], capture_output=True)
    for f in [frag, overlay]:
        if os.path.exists(f): os.remove(f)
    if r.returncode != 0:
        raise Exception("seg " + str(num) + ": " + r.stderr.decode()[-600:])
    print("  OK segmento " + str(num))
    return out

# ============================================================
# Publicar
# ============================================================
def publicar_instagram(video_path, caption, usuario):
    session_data = json.loads(os.environ["INSTAGRAM_SESSION_JSON"])
    session_path = "/tmp/sesion_instagram.json"
    with open(session_path, "w") as f:
        json.dump(session_data, f)
    cl = Client()
    cl.load_settings(session_path)
    cl.username = usuario
    cl.clip_upload(video_path, caption)
    print("✅ Publicado en Instagram")

def publicar_youtube(video_path, titulo, descripcion):
    SCOPES     = ["https://www.googleapis.com/auth/youtube.upload"]
    token_data = json.loads(os.environ["YT_TOKEN_JSON"])
    creds      = Credentials.from_authorized_user_info(token_data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title":       titulo[:100],
            "description": descripcion,
            "tags":        ["shorts", "humor", "viral", "espana"],
            "categoryId":  "24",
        },
        "status": {
            "privacyStatus":           "public",
            "selfDeclaredMadeForKids": False,
        }
    }
    media    = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
    req      = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = req.next_chunk()
    print("✅ Publicado en YouTube Shorts: https://youtube.com/shorts/" + response["id"])

# ============================================================
# Ciclo principal
# ============================================================
def ejecutar_ciclo():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    blacklist     = cargar_blacklist(BLACKLIST_PATH)
    videos        = buscar_videos_virales(YOUTUBE_API_KEY, blacklist)
    video_elegido = random.choice(videos[:5])
    print("Elegido: " + video_elegido["titulo"])

    comentarios_raw      = obtener_comentarios(video_elegido["id"], YOUTUBE_API_KEY)
    guion                = generar_guion(video_elegido, comentarios_raw, GEMINI_API_KEY, N_COMENTARIOS)
    video_original       = descargar_video(video_elegido["url"], OUTPUT_DIR)
    video_proc, proc_dur = preprocesar_video(video_original, OUTPUT_DIR, N_COMENTARIOS, DURACION_CLIP)

    tramo   = proc_dur / N_COMENTARIOS
    offsets = [max(0, round(random.uniform(i*tramo, (i+1)*tramo - DURACION_CLIP - 1), 2))
               for i in range(N_COMENTARIOS)]

    print("Montando " + str(N_COMENTARIOS) + " segmentos...")
    segmentos = []
    for c, offset in zip(guion["comentarios"], offsets):
        seg = montar_segmento(video_proc, c["comentario"], c["autor"], c["likes"],
                              c["num"], OUTPUT_DIR, DURACION_CLIP, offset)
        segmentos.append(seg)

    fecha_str   = datetime.now().strftime("%d_%m_%Y_%H%M")
    video_final = OUTPUT_DIR + "final_" + fecha_str + ".mp4"
    lista       = OUTPUT_DIR + "list.txt"
    with open(lista, "w") as f:
        for s in segmentos:
            f.write("file '" + s + "'\n")
    r = subprocess.run([
        "ffmpeg","-y","-f","concat","-safe","0","-i",lista,
        "-c:v","libx264","-c:a","aac","-pix_fmt","yuv420p", video_final
    ], capture_output=True)
    if r.returncode != 0:
        raise Exception("concat: " + r.stderr.decode()[-400:])
    print("Video final: " + str(round(get_duracion(video_final), 1)) + "s")

    blacklist.add(video_elegido["id"])
    guardar_blacklist(BLACKLIST_PATH, blacklist)

    publicar_instagram(video_final, guion["caption"], INSTAGRAM_USER)
    titulo_yt = " ".join([w for w in guion["caption"].split() if not w.startswith("#")])[:80]
    publicar_youtube(video_final, titulo_yt, guion["caption"])

    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    print("Caption: " + guion["caption"])

if __name__ == "__main__":
    ejecutar_ciclo()
