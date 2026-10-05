# SA Army — Energi Elite AI Intern Challenge (Door B)

An AI-powered system to find, sign up, and activate car dealership Sales Advisors (SAs) across KL & Selangor — so they refer home charger leads for RM200 commission.

## What it does

- **Target dealerships** across KL/Selangor
- **AI-generated personalized outreach** to SAs (human-approved before sending)
- **Self sign-up flow** for SAs with unique referral codes
- **WhatsApp lead capture** — SAs send a lead in 30 seconds, AI parses it automatically
- **Commission tracking** — SAs check their own earnings anytime
- **Admin dashboard** with leads, commissions, and activity log

## Tech stack

- Python 3.12
- Streamlit (UI)
- SQLite (database)
- OpenRouter (free tier)

## Setup

```bash
# 1. Clone repo
git clone https://github.com/hsnaz0808/sa-army.git
cd sa-army

# 2. Create virtual environment
python -m venv venv
venv\Scripts\activate    # Windows
# source venv/bin/activate  # Mac/Linux

# 3. Install dependencies
pip install streamlit openai pandas python-dotenv

# 4. Run
streamlit run app.py
