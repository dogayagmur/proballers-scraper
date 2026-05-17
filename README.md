proballers-scraper
A Python-based data extraction pipeline using undetected-chromedriver to handle Cloudflare clearance and extracting comprehensive basketball statistics and game logs for sports analytics.

🏀 Overview
This repository contains a full automated web scraping pipeline designed to extract a decade of historical basketball game logs (2014–2024). It handles complex anti-bot protection and seamlessly inserts the extracted data directly into a database for downstream analytics.

The resulting datasets are structured to support advanced sports data science applications, including unsupervised machine learning for player clustering, AI-driven recommendation systems, and the calculation of dynamic Weighted Efficiency Ratings (WER) for positionless basketball scouting.

✨ Key Features
Cloudflare Evasion: Utilizes undetected-chromedriver and selenium to successfully bypass Cloudflare turnstiles and bot-detection mechanisms.

Comprehensive Extraction: Targets and parses detailed game-to-game statistical logs.

Automated Database Pipeline: Includes scripts to handle bulk data insertion and schema management, ensuring the data is immediately ready for querying and modeling.

Historical Scale: Configured to handle large-volume scraping across multiple seasons.

🛠️ Prerequisites
Python 3.8+

Google Chrome (latest version)

Database (e.g., PostgreSQL/MySQL/SQLite)
