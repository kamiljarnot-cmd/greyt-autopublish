#!/usr/bin/env python3
"""
Greyt.pl — Auto Publisher
Wybiera najwyższy priorytet z CSV, generuje wpis przez Claude API,
generuje obrazki przez Replicate (Flux Dev), publikuje na WordPress.
"""

import os
import csv
import time
import unicodedata
import requests
import anthropic
from datetime import datetime
from pathlib import Path

# ──────────────────────────────────────────
# KONFIGURACJA
# ──────────────────────────────────────────

WP_URL            = "https://greyt.pl/wp-json/wp/v2"
WP_USER           = os.environ["WP_USER_GREYT"]
WP_PASSWORD       = os.environ["WP_PASSWORD_GREYT"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
REPLICATE_API_KEY = os.environ["REPLICATE_API_KEY"]

CSV_PATH = Path("keywords.csv")

SEASON_MAP = {
    "wiosna":     [3, 4, 5],
    "lato":       [6, 7, 8],
    "jesień":     [9, 10, 11],
    "zima":       [12, 1, 2],
    "całoroczny": list(range(1, 13)),
}

# Kategorie bloga greyt.pl
BLOG_CATEGORIES = {
    "inspiracje":  152,  # Inspiracje i aranżacje
    "poradniki":   153,  # Poradniki i montaż
    "ogrod":       154,  # Ogród i taras
    "wnetrza":     155,  # Wnętrza z betonu
    "aktualnosci": 156,  # Aktualności i nowości
}

DEFAULT_CATEGORY_ID = 152  # Inspiracje i aranżacje

# ──────────────────────────────────────────
# SYSTEM PROMPT — głos Dominika
# ──────────────────────────────────────────

def get_system_prompt() -> str:
    year = datetime.now().year
    return f"""Jesteś Dominikiem — właścicielem firmy Greyt, która produkuje i sprzedaje wyroby z betonu architektonicznego: donice betonowe, płyty betonowe ścienne i nawierzchniowe oraz zegary ścienne betonowe. Piszesz blog dla swoich klientów.

GŁOS I STYL:
- Ekspercki ale przystępny — piszesz do właścicieli domów, architektów, projektantów wnętrz
- Naturalny, bezpośredni, z pasją do betonu i wzornictwa
- Konkretne porady, praktyczne wskazówki, inspiracje
- Piszesz w pierwszej osobie jako Dominik lub "my w Greyt"
- Podkreślasz jakość, trwałość i estetykę betonu architektonicznego

FORMAT WPISU:
- Wciągający wstęp (2-3 zdania, bez nagłówka)
- 3-5 sekcji z nagłówkami <h2>
- Opcjonalne <h3> wewnątrz sekcji
- Podsumowanie lub CTA na końcu (zachęć do kontaktu lub zakupu)
- Minimum 700 słów
- Cały wpis w HTML: <p>, <h2>, <h3>, <ul>, <li>, <strong>
- NIE używaj znaczników ```html ani żadnych backtick — zwróć czysty HTML

LINKOWANIE:
- W treści umieść 1-3 linki do kategorii sklepu w formacie: <a href="URL">nazwa</a>
- Linki wplataj naturalnie w treść
- Używaj TYLKO URLi które dostaniesz w prompcie — nie wymyślaj własnych

SEO:
- Fraza kluczowa musi pojawić się w pierwszym akapicie
- Fraza kluczowa w co najmniej jednym <h2>
- Naturalnie rozsiana przez cały tekst

AKTUALNY ROK: {year}. Zawsze używaj roku {year} gdy piszesz o aktualnych trendach. Nigdy nie pisz o poprzednich latach jako aktualnych."""


# ──────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────

def get_current_season() -> str:
    month = datetime.now().month
    for season, months in SEASON_MAP.items():
        if month in months:
            return season
    return "całoroczny"


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("ł", "l")
    text = text.replace(" ", "-")
    return text[:40]


def load_keywords() -> list[dict]:
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_keywords(rows: list[dict]) -> None:
    fieldnames = ["Fraza kluczowa", "Sezon", "Priorytet", "Kategoria", "Status", "Link do wpisu"]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def pick_keyword(rows: list[dict]) -> dict | None:
    season = get_current_season()
    priority_order = {"wysoki": 0, "średni": 1, "niski": 2}

    candidates = [
        r for r in rows
        if r["Status"].strip() == "do wykorzystania"
        and (r["Sezon"].strip() == season or r["Sezon"].strip() == "całoroczny")
    ]

    if not candidates:
        candidates = [r for r in rows if r["Status"].strip() == "do wykorzystania"]

    if not candidates:
        return None

    candidates.sort(key=lambda r: priority_order.get(r["Priorytet"].strip(), 99))
    return candidates[0]


def get_product_categories() -> list[dict]:
    try:
        resp = requests.get(
            f"{WP_URL}/product_cat?per_page=100",
            auth=(WP_USER, WP_PASSWORD),
            timeout=15
        )
        if resp.ok and resp.text.strip():
            return resp.json()
        print(f"⚠️  product_cat status: {resp.status_code}")
    except Exception as e:
        print(f"⚠️  Błąd pobierania kategorii: {e}")
    return []


def format_categories_for_prompt(cats: list[dict]) -> str:
    lines = []
    for c in sorted(cats, key=lambda x: x.get("name", "")):
        name = c.get("name", "")
        link = c.get("link", c.get("url", ""))
        if name and link and "Wszystkie" not in name:
            lines.append(f"- {name}: {link}")
    return "\n".join(lines)


def resolve_blog_category(keyword_row: dict) -> int:
    """Dobiera ID kategorii bloga na podstawie kolumny Kategoria w CSV."""
    cat_key = keyword_row.get("Kategoria", "").strip().lower()
    return BLOG_CATEGORIES.get(cat_key, DEFAULT_CATEGORY_ID)


def generate_post(keyword: str, categories_text: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_prompt = f"""Napisz wpis blogowy dla greyt.pl.

FRAZA KLUCZOWA: {keyword}

DOSTĘPNE KATEGORIE PRODUKTÓW (używaj TYLKO tych URLi):
{categories_text}

Wybierz 1-3 kategorie które pasują do tematu wpisu i naturalnie wpleć linki w treść.

Na końcu wpisu (po treści HTML) dodaj w osobnych liniach:
WP_TITLE: [tytuł wpisu do WordPressa, naturalny, przyciągający uwagę]
SEO_TITLE: [tytuł SEO max 60 znaków, zawiera frazę kluczową na początku]
SEO_DESC: [opis meta max 155 znaków, zawiera frazę kluczową, zachęca do kliknięcia, kończy się kropką]
IMAGE_PROMPT_FEATURED: [prompt po angielsku do głównego zdjęcia, opisz scenę z betonem architektonicznym: concrete planter, architectural concrete, modern garden or interior, natural daylight, minimalist style, warm neutral tones, 16:9, no text, photorealistic]
IMAGE_PROMPT_1: [prompt po angielsku do zdjęcia w treści, inna scena z betonem — płyty, zegar lub inna dekoracja betonowa, ten sam styl minimalistyczny, 16:9, no text, photorealistic]
IMAGE_ALT_1: [opis alt po polsku dla zdjęcia w treści, max 10 słów, zawiera frazę kluczową]"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=get_system_prompt(),
        messages=[{"role": "user", "content": user_prompt}]
    )

    raw = message.content[0].text
    lines = raw.strip().split("\n")

    wp_title = keyword
    seo_title = ""
    seo_desc = ""
    image_prompt_featured = ""
    image_prompt_1 = ""
    image_alt_1 = ""
    content_lines = []

    for line in lines:
        if line.startswith("WP_TITLE:"):
            wp_title = line.replace("WP_TITLE:", "").strip()
        elif line.startswith("SEO_TITLE:"):
            seo_title = line.replace("SEO_TITLE:", "").strip()
        elif line.startswith("SEO_DESC:"):
            seo_desc = line.replace("SEO_DESC:", "").strip()
        elif line.startswith("IMAGE_PROMPT_FEATURED:"):
            image_prompt_featured = line.replace("IMAGE_PROMPT_FEATURED:", "").strip()
        elif line.startswith("IMAGE_PROMPT_1:"):
            image_prompt_1 = line.replace("IMAGE_PROMPT_1:", "").strip()
        elif line.startswith("IMAGE_ALT_1:"):
            image_alt_1 = line.replace("IMAGE_ALT_1:", "").strip()
        else:
            content_lines.append(line)

    content = "\n".join(content_lines).strip()

    if len(seo_title) > 60:
        seo_title = seo_title[:57] + "..."
    if len(seo_desc) > 155:
        seo_desc = seo_desc[:152] + "..."

    return {
        "title": wp_title,
        "content": content,
        "seo_title": seo_title,
        "seo_desc": seo_desc,
        "image_prompt_featured": image_prompt_featured,
        "image_prompt_1": image_prompt_1,
        "image_alt_1": image_alt_1,
    }


def generate_image(prompt: str) -> bytes | None:
    prompt = prompt + ", hyperrealistic, professional architectural photography, 4k, no illustrations"
    try:
        headers = {
            "Authorization": f"Bearer {REPLICATE_API_KEY}",
            "Content-Type": "application/json",
            "Prefer": "wait"
        }
        payload = {
            "input": {
                "prompt": prompt,
                "aspect_ratio": "16:9",
                "output_format": "jpg",
                "output_quality": 85,
                "num_outputs": 1,
            }
        }

        for attempt in range(3):
            resp = requests.post(
                "https://api.replicate.com/v1/models/black-forest-labs/flux-dev/predictions",
                headers=headers,
                json=payload,
                timeout=60
            )

            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 15)
                print(f"⏳ Rate limit — czekam {retry_after}s... (próba {attempt + 1}/3)")
                time.sleep(retry_after + 3)
                continue

            if not resp.ok:
                print(f"⚠️  Replicate błąd {resp.status_code}: {resp.text[:300]}")
                return None
            break
        else:
            print("⚠️  Rate limit — nie udało się po 3 próbach")
            return None

        data = resp.json()

        for _ in range(30):
            if data.get("status") in ("succeeded", "failed"):
                break
            time.sleep(3)
            poll = requests.get(
                f"https://api.replicate.com/v1/predictions/{data['id']}",
                headers=headers,
                timeout=15
            )
            data = poll.json()

        if data.get("status") != "succeeded":
            print(f"⚠️  Replicate nie wygenerował obrazka: {data.get('status')}")
            return None

        output = data["output"]
        image_url = output if isinstance(output, str) else output[0]
        print(f"✅ Obrazek wygenerowany: {image_url}")

        img_resp = requests.get(image_url, timeout=30)
        if img_resp.ok:
            return img_resp.content
        print(f"⚠️  Błąd pobierania obrazka: {img_resp.status_code}")
        return None

    except Exception as e:
        print(f"⚠️  Błąd generowania obrazka: {e}")
        return None


def upload_image_to_wp(image_bytes: bytes, filename: str) -> dict | None:
    try:
        resp = requests.post(
            f"{WP_URL}/media",
            auth=(WP_USER, WP_PASSWORD),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "image/jpeg",
            },
            data=image_bytes,
            timeout=30
        )
        if resp.ok:
            data = resp.json()
            print(f"✅ Obrazek uploadowany, media ID: {data['id']}")
            return {"id": data["id"], "url": data["source_url"]}
        print(f"⚠️  Upload błąd {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"⚠️  Błąd uploadu: {e}")
        return None


def insert_image_in_content(content: str, img_tag: str) -> str:
    sections = content.split("</h2>")
    if len(sections) < 2:
        mid = len(content) // 2
        return content[:mid] + img_tag + content[mid:]
    mid_index = len(sections) // 2
    sections[mid_index] = sections[mid_index] + img_tag
    return "</h2>".join(sections)


def publish_post(title: str, content: str, category_id: int, featured_media: int | None = None) -> dict:
    payload = {
        "title": title,
        "content": content,
        "status": "publish",
        "categories": [category_id],
    }
    if featured_media:
        payload["featured_media"] = featured_media

    resp = requests.post(
        f"{WP_URL}/posts",
        auth=(WP_USER, WP_PASSWORD),
        json=payload,
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def update_yoast(post_id: int, seo_title: str, seo_desc: str) -> None:
    resp = requests.post(
        f"{WP_URL}/posts/{post_id}",
        auth=(WP_USER, WP_PASSWORD),
        json={
            "meta": {
                "_yoast_wpseo_title": seo_title,
                "_yoast_wpseo_metadesc": seo_desc,
            }
        },
        timeout=15
    )
    resp.raise_for_status()


# ──────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Start auto-publishera Greyt.pl")

    # 1. Keyword
    rows = load_keywords()
    keyword_row = pick_keyword(rows)

    if not keyword_row:
        print("❌ Brak dostępnych słów kluczowych.")
        return

    keyword = keyword_row["Fraza kluczowa"]
    blog_cat_id = resolve_blog_category(keyword_row)
    print(f"✅ Keyword: {keyword} (sezon: {keyword_row['Sezon']}, priorytet: {keyword_row['Priorytet']})")
    print(f"📂 Kategoria bloga ID: {blog_cat_id}")

    # 2. Kategorie produktów
    print("📦 Pobieram kategorie produktów...")
    cats = get_product_categories()
    cats_text = format_categories_for_prompt(cats)
    if not cats_text:
        print("⚠️  Brak kategorii — kontynuuję bez linków")

    # 3. Generuj wpis
    print("✍️  Generuję wpis przez Claude API...")
    post_data = generate_post(keyword, cats_text)
    print(f"📝 Tytuł: {post_data['title']}")
    print(f"🔍 SEO title ({len(post_data['seo_title'])} zn.): {post_data['seo_title']}")
    print(f"📄 SEO desc ({len(post_data['seo_desc'])} zn.): {post_data['seo_desc']}")

    slug_base = slugify(keyword)
    date_str = datetime.now().strftime('%Y%m%d')
    content = post_data["content"]

    # 4. Generuj featured image
    featured_media_id = None
    if post_data["image_prompt_featured"]:
        print("🎨 Generuję featured image...")
        img_bytes = generate_image(post_data["image_prompt_featured"])
        if img_bytes:
            result = upload_image_to_wp(img_bytes, f"{slug_base}-{date_str}.jpg")
            if result:
                featured_media_id = result["id"]

    # 5. Generuj obrazek do treści
    time.sleep(5)
    print("🎨 Generuję obrazek do treści...")
    img1_bytes = generate_image(post_data["image_prompt_1"]) if post_data["image_prompt_1"] else None
    if img1_bytes:
        result1 = upload_image_to_wp(img1_bytes, f"{slug_base}-1-{date_str}.jpg")
        if result1:
            img_tag = f'<figure class="wp-block-image"><img src="{result1["url"]}" alt="{post_data["image_alt_1"]}" /></figure>'
            content = insert_image_in_content(content, img_tag)

    # 6. Publikuj wpis
    print("🚀 Publikuję na WordPress...")
    published = publish_post(post_data["title"], content, blog_cat_id, featured_media=featured_media_id)
    post_id = published["id"]
    post_link = published["link"]
    print(f"✅ Opublikowano! ID: {post_id}, URL: {post_link}")

    # 7. Yoast SEO
    time.sleep(2)
    print("🔧 Aktualizuję Yoast SEO...")
    try:
        update_yoast(post_id, post_data["seo_title"], post_data["seo_desc"])
        print("✅ Yoast zaktualizowany")
    except Exception as e:
        print(f"⚠️  Yoast błąd (dodaj snippet PHP): {e}")

    # 8. Zaktualizuj CSV
    for row in rows:
        if row["Fraza kluczowa"] == keyword:
            row["Status"] = "wykorzystana"
            row["Link do wpisu"] = post_link
            break
    save_keywords(rows)
    print("✅ CSV zaktualizowany")

    print(f"\n🎉 Gotowe! Wpis: {post_link}")


if __name__ == "__main__":
    main()
