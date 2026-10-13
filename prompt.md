You are a Python engineer. Write a complete, runnable Python script that finds emerging women's clothing brand leads on Instagram, matching this exact brief:

TARGET
- Two groups: USA-based and India-based emerging women's clothing brands.
- "Emerging": newly launched / small — ideally under 5,000 Instagram followers, hard cap under 10,000.
- Style: contemporary / western womenswear. Exclude bridal-only, kids' wear, menswear-only, and pure reseller/curated boutiques.
- Prefer brands likely to need sampling, low-MOQ production, garment development, or fabric sourcing. Strong signals: small-batch, handmade, made-to-order, slow fashion.
- For the US group: prefer Indian/South Asian-founded brands or brands using Indian fabrics, textiles, prints, embroidery, or craftsmanship.

DATA SOURCE: Instagram Graph API (v18+). Read the access token from env var IG_ACCESS_TOKEN. Use:
1. Hashtag search over a configurable tag list (e.g. #emergingdesigner, #smallfashionbrand, #slowfashionindia, #desiboutique, #handmadewomenswear, #lowmoq, #madeinindia, #shopsmallusa).
2. Business Discovery (/{ig-user-id}?fields=business_discovery.username(...)...) to pull: username, name, biography, followers_count, follows_count, media_count, website.
3. For each candidate, fetch the last ~5 media (/{username}/media?fields=timestamp,caption) and mark ACTIVE only if the newest post is within the last 60 days.
4. Region inference: match bio/website text against configurable USA_KEYWORDS and INDIA_KEYWORDS lists; skip when region is unverifiable or mismatched.
5. Founder extraction: parse bio for patterns like "Founder:", "by X", "CEO".

FILTERS (enforced in code):
- followers_count < 10000 (tag "ideal" when < 5000)
- media_count >= 3
- active within ACTIVITY_WINDOW_DAYS (default 60)
- bio must not contain exclusion keywords (configurable)

DEDUPE: maintain seen_brands.csv (username, date_found). Skip any handle already in it; append new finds at the end of the run.

RATE LIMITS: Instagram returns HTTP 429 under load. Implement exponential backoff with jitter, batch profile lookups in groups of ~20, sleep 1–2 seconds between batches, and log every skip with its reason.

OUTPUT: usa_leads.csv and india_leads.csv with columns: brand_name, instagram_url, followers, founder, website, bio_snippet, last_post_date. Print the final count per region. Never pad with unverified entries — if fewer qualify, say so plainly.

CONFIG block at the top of the file: token from env, HASHTAGS, FOLLOWER_CAP, IDEAL_CAP, ACTIVITY_WINDOW_DAYS, region keywords, exclusion keywords, BATCH_SIZE, output paths. argparse flags: --region (usa|india|both), --max-results.

Also print a short setup note: how to get the token (Meta for Developers → app with instagram_graph_user_profile + instagram_graph_user_media scopes), and note that Business Discovery only works for business/creator accounts — personal accounts get skipped and logged.

Dependencies: `requests` only. Include `if __name__ == "__main__":`.