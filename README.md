# The Leonida Daily: Instagram bot

Finds fresh **GTA6** news, updates, guides and tips, has Claude pick the best stories and write
the posts, designs branded carousels (and Reels for the strongest stories) on top of **your own
screenshots**, sends them to you on Telegram for approval, then posts them to Instagram.

It's the same engine as the Cycling News Today bot. Only `config.yaml` differs.

## How it runs
- A Cloudflare Worker (code in `cloudflare/worker.js`) starts the GitHub workflow at :07 and :37
  past each hour, and within a minute of you tapping a button or sending a command on Telegram.
- Each run reads Telegram, posts the next approved item if a slot is free, and scouts for news
  every `scout_every_hours`.

## Secrets (Settings > Secrets and variables > Actions)
`ANTHROPIC_API_KEY`, `CLOUDINARY_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`,
`INSTAGRAM_APP_ID` (the Instagram account ID), `INSTAGRAM_APP_TOKEN`, `TELEGRAM_BOT_ID`
(your chat ID with this page's Telegram bot), `TELEGRAM_BOT_TOKEN`.

## First run
1. **Actions > The Leonida Daily bot > Run workflow > check**: tests every connection and reports to Telegram.
2. **Run workflow > scout**: sends your first drafts.

## Screenshot backgrounds
Upload screenshots **you took yourself** to the Cloudinary folder named in `config.yaml`
(`design.background_images.cloudinary_folder`, currently `gta6`). Each post picks one at random
(avoiding recent repeats) and darkens it so the text stays readable. Remove that setting to
use plain backgrounds. Don't use trailer stills or other people's images: they can lead to
takedowns and count as unoriginal content on Instagram.

## Telegram commands
`/status`, `/scout`, `/pause`, `/resume`.

## Settings
Everything else (name, tone, focus, topics to avoid, colours, posting hours, limits, Reels,
news sources and the keyword filter) is in `config.yaml`.
