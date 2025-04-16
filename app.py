import streamlit as st
import requests
from bs4 import BeautifulSoup
import pandas as pd
import plotly.express as px
from datetime import datetime
import time
# import os # No longer needed for file path checking
import re
from urllib.parse import urljoin, urlparse
from streamlit_gsheets import GSheetsConnection # <-- Import GSheetsConnection

# --- Instagram Data Fetching ---
try:
    import instaloader
except ImportError:
    st.error("Instaloader library not found. Please install it: pip install instaloader")
    st.stop()

# --- Configuration ---
TARGET_URL = "https://www.chapterhostels.com/" # Assuming this is the general brand URL
INSTAGRAM_USERNAME = "chapterhostels" # Make sure this is the correct username
# DATA_FILE = "monitoring_data.csv" # <-- No longer needed
WORKSHEET_NAME = "MonitoringData" # Name of the tab in your Google Sheet

# Found via inspecting chaptersanfrancisco.com
LOGO_URL = "https://www.chaptersanfrancisco.com/assets/B/themes/chaptersanfrancisco-new/img/logo-new.png"

REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36'
}

# Define expected columns for the data - REMOVED SEMRUSH & GOOGLE INDEX
ALL_COLUMNS = [
    'Timestamp', 'URL', 'Instagram Handle', 'Title', 'Meta Description',
    'Robots.txt Exists', 'Sitemap Found', 'H1 Tags',
    'Followers', 'Following', 'Posts'
]

# Columns expected to be numeric for plotting/analysis - REMOVED SEMRUSH
NUMERIC_COLUMNS = ['Followers', 'Following', 'Posts']


# --- Helper Functions (fetch_website_data and get_soup remain the same) ---

def get_soup(url):
    """Fetches URL content and returns BeautifulSoup object."""
    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
        response.raise_for_status()
        return BeautifulSoup(response.content, 'lxml', from_encoding='utf-8')
    except requests.exceptions.Timeout:
        st.warning(f"Timeout fetching {url}")
        return None
    except requests.exceptions.RequestException as e:
        st.warning(f"Could not fetch {url}: {e}")
        return None
    except Exception as e:
        st.error(f"Error parsing HTML from {url}: {e}")
        return None


def fetch_website_data(url):
    """Fetches basic SEO data from the website."""
    data = {
        "Title": "N/A",
        "Meta Description": "N/A",
        "Robots.txt Exists": False,
        "Sitemap Found": "N/A",
        "H1 Tags": []
    }
    st.write(f"Fetching website data from {url}...")
    soup = get_soup(url)
    if not soup: return data

    title_tag = soup.find('title')
    if title_tag and title_tag.string: data["Title"] = title_tag.string.strip()

    og_desc = soup.find('meta', property='og:description')
    if og_desc and og_desc.get('content'):
        data["Meta Description"] = og_desc.get('content').strip()
    else:
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc and meta_desc.get('content'):
            data["Meta Description"] = meta_desc.get('content').strip()

    h1_tags = soup.find_all('h1')
    data["H1 Tags"] = [h1.text.strip() for h1 in h1_tags if h1.text.strip()]

    robots_url = urljoin(url, "/robots.txt")
    try:
        robots_response = requests.get(robots_url, headers=REQUEST_HEADERS, timeout=10)
        if robots_response.status_code == 200:
            data["Robots.txt Exists"] = True
            sitemap_links = re.findall(r'Sitemap:\s*(.*)', robots_response.text, re.IGNORECASE)
            if sitemap_links:
                data["Sitemap Found"] = ", ".join([link.strip() for link in sitemap_links])
            else:
                data["Sitemap Found"] = "Directive not found in robots.txt"
        else:
             pass

    except requests.exceptions.RequestException as e:
        st.warning(f"Could not check {robots_url}: {e}")


    if not data["Sitemap Found"] or "Directive not found" in data["Sitemap Found"] or data["Sitemap Found"] == "N/A":
        common_sitemaps = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap", "/sitemap.php"]
        found_sm = False
        found_urls = []
        for smap in common_sitemaps:
            sitemap_url = urljoin(url, smap)
            try:
                sitemap_response = requests.head(sitemap_url, headers=REQUEST_HEADERS, timeout=7, allow_redirects=True)
                if sitemap_response.status_code == 200:
                    found_urls.append(sitemap_url)
                    found_sm = True
            except requests.exceptions.RequestException:
                continue

        if found_sm:
             data["Sitemap Found"] = ", ".join(found_urls)
        elif data["Sitemap Found"] == "Directive not found in robots.txt" or data["Sitemap Found"] == "N/A":
             data["Sitemap Found"] = "Not found (checked robots.txt & common paths)"

    return data

# --- Instagram Data Fetching Function (fetch_instagram_data remains the same) ---
@st.cache_data(ttl=3600) # Cache for 1 hour
def fetch_instagram_data(username):
    """Fetches Instagram profile data using Instaloader."""
    data = {"Followers": pd.NA, "Following": pd.NA, "Posts": pd.NA}
    st.write(f"Fetching Instagram data for @{username}...")
    try:
        L = instaloader.Instaloader(
            user_agent=REQUEST_HEADERS['User-Agent'],
            quiet=True,
            compress_json=False,
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False
            )
        profile = instaloader.Profile.from_username(L.context, username)
        data["Followers"] = profile.followers
        data["Following"] = profile.followees
        data["Posts"] = profile.mediacount
        st.success(f"Successfully fetched Instagram data for @{username}")
    except instaloader.exceptions.ProfileNotFoundError:
        st.error(f"Instagram profile @{username} not found.")
    except instaloader.exceptions.LoginRequiredException:
        st.error(f"Login required to fetch data for @{username}. Instaloader session file might be needed.")
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        st.error(f"Profile @{username} is private and not followed by the Instaloader session.")
    except instaloader.exceptions.ConnectionException as e:
        st.error(f"Connection error fetching Instagram data: {e}. Might be rate-limited or network issue.")
    except Exception as e:
        st.error(f"An unexpected error occurred fetching Instagram data for @{username}: {e}")
    finally:
        for key in data:
            if data[key] is None: data[key] = pd.NA
        return data


# --- Data Loading and Saving Functions (MODIFIED FOR GOOGLE SHEETS) ---

def load_historical_data():
    """Loads historical data from Google Sheet, ensuring columns/types."""
    st.write(f"Connecting to Google Sheet '{WORKSHEET_NAME}' to load data...")
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        # Read data, specify worksheet, usecols to avoid extra empty cols, cache for 5 secs
        df = conn.read(worksheet=WORKSHEET_NAME, usecols=list(range(len(ALL_COLUMNS))), ttl=5)
        df = df.dropna(how='all') # Drop fully empty rows often present in sheets

        if df.empty:
             st.warning("Google Sheet appears empty. Returning empty DataFrame.")
             df = pd.DataFrame(columns=ALL_COLUMNS)
        else:
             st.write(f"Loaded {len(df)} rows from Google Sheet.")

        # --- Data Validation and Type Conversion ---
        # Ensure all expected columns exist, fill with NA if missing
        for col in ALL_COLUMNS:
            if col not in df.columns:
                if col in NUMERIC_COLUMNS:
                    df[col] = pd.NA
                else:
                    df[col] = pd.NA

        # Ensure Timestamp is datetime
        if 'Timestamp' in df.columns:
             df['Timestamp'] = pd.to_datetime(df['Timestamp'], errors='coerce')
             df = df.dropna(subset=['Timestamp']) # Critical: drop rows where timestamp failed conversion
        else:
             st.error("Timestamp column missing from Google Sheet data!")
             # Return empty df if timestamp is missing
             df = pd.DataFrame(columns=ALL_COLUMNS)
             df['Timestamp'] = pd.to_datetime(df['Timestamp'])


        # Coerce numeric types
        for col in NUMERIC_COLUMNS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # Handle H1 Tags (already stored as string)
        if 'H1 Tags' in df.columns:
            df['H1 Tags'] = df['H1 Tags'].astype(str).fillna(pd.NA) # Ensure string, keep NA

        return df[ALL_COLUMNS] # Ensure column order

    except Exception as e:
        st.error(f"Error connecting to or reading from Google Sheet: {e}")
        st.warning("Could not load historical data. Starting fresh for this session.")
        # Return empty df matching schema on error
        df = pd.DataFrame(columns=ALL_COLUMNS)
        df['Timestamp'] = pd.to_datetime(df['Timestamp'])
        for col in NUMERIC_COLUMNS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        return df


def save_historical_data(data_dict):
    """Appends new data to the Google Sheet, ensuring schema consistency."""
    st.write("Loading existing data from Google Sheet before saving...")
    df_history = load_historical_data() # Load current data from Sheet

    # --- Prepare New Data Row ---
    new_data_df = pd.DataFrame([data_dict])
    new_data_df['Timestamp'] = pd.to_datetime(new_data_df['Timestamp'], errors='coerce')

    # Ensure all expected columns exist in the new row
    for col in ALL_COLUMNS:
        if col not in new_data_df.columns:
            if col in NUMERIC_COLUMNS:
                new_data_df[col] = pd.NA
            else:
                new_data_df[col] = pd.NA

    # Reorder columns to match ALL_COLUMNS
    new_data_df = new_data_df[ALL_COLUMNS]

    # Convert numeric columns in the new row
    for col in NUMERIC_COLUMNS:
        if col in new_data_df.columns:
            new_data_df[col] = pd.to_numeric(new_data_df[col], errors='coerce')

    # Handle H1 Tags list - MUST save as string to Sheets
    if 'H1 Tags' in new_data_df.columns:
         new_data_df['H1 Tags'] = new_data_df['H1 Tags'].apply(
             lambda x: str(x) if isinstance(x, list) else str(x) # Convert list/other to string
         )

    # --- Combine and Save ---
    # Replace Pandas NA with None for Sheets compatibility (often becomes empty cell)
    df_history_safe = df_history.astype(object).where(pd.notnull(df_history), None)
    new_data_df_safe = new_data_df.astype(object).where(pd.notnull(new_data_df), None)

    st.write("Appending new data row...")
    df_updated = pd.concat([df_history_safe, new_data_df_safe], ignore_index=True)
    df_updated = df_updated.sort_values(by="Timestamp", ascending=True)

    # Ensure Timestamp is suitable for Sheets (string might be safest)
    df_updated['Timestamp'] = df_updated['Timestamp'].astype(str)

    st.write(f"Connecting to Google Sheet '{WORKSHEET_NAME}' to save data...")
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        # Overwrite the entire sheet with the updated dataframe
        conn.update(worksheet=WORKSHEET_NAME, data=df_updated)
        st.write("Data successfully saved to Google Sheet.")
    except Exception as e:
        st.error(f"Failed to save data to Google Sheet: {e}")
        st.error("The latest data point might not have been saved.")


# --- Streamlit App Layout (Mostly Unchanged) ---

st.set_page_config(page_title="Chapter Hostels Presence", layout="wide")

# --- Header Section ---
col_logo, col_title = st.columns([1, 5])
with col_logo:
    st.image(LOGO_URL, width=150)
with col_title:
    st.title("Chapter Hostels: Online Presence & Strategy")
    st.caption("Social Media Growth Plan & Basic Monitoring Dashboard")

# --- Create Tabs ---
tab1, tab2 = st.tabs(["📈 Social Media Growth Plan", "📊 Monitoring Dashboard"])

# --- Tab 1: Social Media Plan (Unchanged) ---
with tab1:
    st.header("Social Media Plan: Follower Growth (Nomad/Tech/Budget Focus)")
    st.markdown("""
        This plan focuses on building a *relevant* follower base for Chapter Hostels, specifically targeting Digital Nomads, the Tech Community, and Budget-Conscious Explorers, using Chapter San Francisco details as a core example.
    """)

    st.subheader("🎯 Goal")
    st.markdown("Significantly increase the total number of **relevant** followers across key social media platforms within the next 6 months, positioning Chapter as the smart, connected, and affordable base in its cities.")

    st.subheader("👥 Target Audience (Primary)")
    st.markdown("""
    *   **Digital Nomads & Remote Workers:** Seeking reliable WiFi, comfortable workspaces, community, and flexible/affordable stays (short-to-medium term). Value convenience and connectivity. (Age 22-45+)
    *   **Tech Community:** Interns, new hires, project workers, conference attendees needing affordable, functional housing in tech hubs. Appreciate tech integration (self-check-in, keyless). (Age 20-40)
    *   **Budget-Conscious Explorers:** Often international travelers prioritizing value, safety, cleanliness, and location over luxury. Seek practical tips and community. (Age 18-35)
    """)
    st.caption("*Secondary Audience:* General young travelers, backpackers, students seeking short stays.")

    st.subheader("📱 Key Platforms & Roles")
    st.markdown("""
    1.  **Instagram (Primary Focus):** Visual proof of facilities (WiFi, booths), community vibe, city access. Drive bookings (short & long stay).
        *   *Content:* High-quality photos/videos (workspaces, rooms), WiFi speed tests, guest testimonials (nomads/techies), practical city guides, tech features (self-check-in), Reels ("productive day at Chapter").
    2.  **TikTok (Awareness & Personality):** Reach younger explorers/tech interns. Showcase practical, efficient side. Quick tips, relatable humor.
        *   *Content:* Fast tours, "SF on a budget," house rules explainers, self-check-in demo, trending sounds related to work/travel/SF.
    3.  **Facebook (Information & Community):** Detailed info hub, event sharing (local tech meetups?), Q&A, targeted ads, potential private group for longer stays.
        *   *Content:* Practical guides (parking, transport), room type details, Tech Housing info, positive reviews, event links.
    4.  **(Consider) LinkedIn (Niche - Tech Housing):** Target tech professionals/companies for longer stays. Position as flexible corporate housing alternative.
        *   *Content:* Professional posts on amenities for workers, cost savings, testimonials. Use sparingly.
    """)

    st.subheader("💡 Content Pillars")
    col_p1, col_p2 = st.columns(2)
    with col_p1:
        st.markdown("""
        1.  **The Connected & Productive Hub:**
            *   Showcase: FAST WIFI, soundproof booths, lounges, power outlets, keyless entry.
            *   Highlight: Quiet hours (positive), cleanliness, security.
            *   *Goal:* Ideal base for work & exploration.
        2.  **Smart Budget Travel:**
            *   Showcase: Clean rooms, kitchenettes, free luggage storage, laundry, public transport access.
            *   Highlight: Free city activities, cheap eats, Clipper card info.
            *   *Goal:* Emphasize value without sacrificing essentials.
        """)
    with col_p2:
        st.markdown("""
        3.  **Respectful Community & Shared Living:**
            *   Showcase: Diverse guests, clean shared spaces, staff (during hours).
            *   Highlight: House rules (quiet, clean, no party) for comfort/safety. International focus.
            *   *Goal:* Attract considerate guests.
        4.  **San Francisco (or City) Insider:**
            *   Showcase: Local experiences, practical navigation (stairs!), neighborhood guides, safety tips.
            *   Highlight: Specific location & access.
            *   *Goal:* Provide local value, position as launchpad.
        5.  **Tech Housing & Longer Stays:**
            *   Showcase: Dedicated info, benefits (utilities, flexibility), testimonials.
            *   *Goal:* Attract qualified leads.
        """)

    st.subheader("🚀 Strategies for Follower Growth")
    st.markdown("""
    1.  **Optimize Profiles:** Clear bio mentioning keywords (Fast WiFi, Work Booths, Budget-Friendly, Nomads, Techies). Strong CTA. Link-in-bio tool. Organized Highlights (Workspaces, WiFi, Rooms, SF Tips, Tech Housing, Rules).
    2.  **Consistent, Targeted Content:** Follow schedule, align with pillars. Prioritize video (Reels/TikTok) showcasing features & tips.
    3.  **Hyper-Engage Niches:** Respond quickly. Engage daily with relevant hashtags (#digitalnomad, #remotework, #sftech, #workfromanywhere, #cheaptravel), influencers & communities. Monitor mentions.
    4.  **Refined Hashtag Strategy:** Mix broad, niche, location, feature, and branded hashtags.
        *   `#chapterhostels` `#chaptersf` `#sanfranciscohostel`
        *   `#digitalnomad` `#remoteworker` `#techlife` `#sfintern` `#budgettravel` `#solotraveler`
        *   `#fastwifi` `#workbooth` `#coliving` `#affordablehousing`
        *   `#sanfrancisco` `#californiatravel` `#japantownsf`
    5.  **Targeted Contests:** Offer relevant prizes (week's stay, tech gadgets, co-working pass). Require follow + tag relevant friends.
    6.  **Strategic Collaborations:** Partner with nomad/tech bloggers/Youtubers, coding bootcamps, local cafes.
    7.  **Promote Offline/Cross-Platform:** Highlight key features (WiFi, booths) on signage, website, emails.
    8.  **Targeted Advertising (Meta Ads):** Use precise targeting (interests, behaviors, job titles), *exclude* local radius. Run follower, traffic, and lead gen (Tech Housing) campaigns.
    """)

    st.subheader("⚠️ Handling Nuances in Comms")
    st.markdown("""
    *   **CA/SF Residents:** Do NOT market to locals. Use ad exclusions. State policy clearly/politely if asked. Frame as "international focus."
    *   **No Elevator/Stairs:** Mention honestly in content/booking info. Frame positively ("authentic SF," "get your steps in!").
    *   **Limited Reception/Self-Service:** Highlight efficiency of self-check-in, tech-forward approach, 24/7 access *after* check-in.
    *   **Non-Party Vibe:** Emphasize "respectful community," "quiet hours," "safe environment" to attract the right guests.
    """)

    st.subheader("📊 Measurement & KPIs")
    st.markdown("""
    *   **Primary:** Total Follower Count & Growth Rate.
    *   **Secondary:** Engagement Rate, Reach/Impressions, Profile Visits, Website Clicks (UTM tracked), Volume/Sentiment of relevant UGC, Follower Demographics, Leads/Inquiries for Tech Housing.
    """)

# --- Tab 2: Monitoring Dashboard (Uses modified load/save functions) ---
with tab2:
    st.header("📊 Basic Monitoring Dashboard")
    st.markdown(f"Monitoring **{TARGET_URL}** and Instagram **@{INSTAGRAM_USERNAME}**")

    # --- Data Fetching Trigger ---
    if st.button("🔄 Fetch Latest Monitoring Data", key="fetch_monitor_data"):
        timestamp = datetime.now()
        st.info(f"Fetching monitoring data at {timestamp.strftime('%Y-%m-%d %H:%M:%S')}...")

        # --- Fetch Website and Instagram Data ---
        website_info = {}
        insta_info = {}

        col_fetch1, col_fetch2 = st.columns(2)

        with col_fetch1:
            with st.spinner("Fetching website data..."):
                website_info = fetch_website_data(TARGET_URL)

        with col_fetch2:
            with st.spinner("Fetching Instagram data..."):
                insta_info = fetch_instagram_data(INSTAGRAM_USERNAME)

        # --- Combine Data ---
        current_data = {
            "Timestamp": timestamp, # Use the timestamp captured at the start
            "URL": TARGET_URL,
            "Instagram Handle": INSTAGRAM_USERNAME,
            "Title": website_info.get("Title", pd.NA),
            "Meta Description": website_info.get("Meta Description", pd.NA),
            "Robots.txt Exists": website_info.get("Robots.txt Exists", False),
            "Sitemap Found": website_info.get("Sitemap Found", pd.NA),
            "H1 Tags": website_info.get("H1 Tags", []), # Keep as list here
            "Followers": insta_info.get("Followers", pd.NA),
            "Following": insta_info.get("Following", pd.NA),
            "Posts": insta_info.get("Posts", pd.NA),
        }

        # Ensure all columns are present before saving
        for col in ALL_COLUMNS:
             if col not in current_data:
                 current_data[col] = pd.NA # Add missing columns as NA

        # Save Data to Google Sheet
        with st.spinner("Saving data to Google Sheet..."):
            save_historical_data(current_data) # <-- Call modified save function

        st.success("Monitoring data fetched and saved successfully to Google Sheet!")
        st.balloons()
        # Clear caches after fetching/saving new data
        st.cache_data.clear()
        st.rerun() # Rerun to ensure the dashboard displays the newly loaded data


    # --- Load and Display Data ---
    st.divider()
    st.subheader("📈 Current Snapshot & Growth Trends")

    # Load data from Google Sheet
    history_df = load_historical_data() # <-- Call modified load function

    if history_df.empty or history_df['Timestamp'].isnull().all():
        st.warning("No historical monitoring data found or loaded correctly from Google Sheet. Click 'Fetch Latest Monitoring Data' to begin.")
    else:
        # Ensure Timestamp is datetime for sorting, handle potential errors again
        history_df['Timestamp'] = pd.to_datetime(history_df['Timestamp'], errors='coerce')
        history_df = history_df.dropna(subset=['Timestamp']) # Drop rows if timestamp failed

        if history_df.empty:
             st.warning("No valid historical data with timestamps found after loading.")
        else:
            # Display Latest Data
            history_df = history_df.sort_values(by="Timestamp", ascending=False)
            latest_data = history_df.iloc[0]

            st.caption(f"Last updated: {latest_data['Timestamp'].strftime('%Y-%m-%d %H:%M:%S') if pd.notna(latest_data['Timestamp']) else 'N/A'}")

            # --- Metrics Display (Unchanged logic, uses loaded data) ---
            col1, col2 = st.columns(2)

            def format_metric(value, format_str="{:,.0f}"):
                if pd.isna(value): return "N/A"
                try: return format_str.format(float(value))
                except (ValueError, TypeError): return str(value)

            with col1:
                st.markdown("##### Instagram")
                st.link_button("View Live Stories ↗", f"https://www.instagram.com/stories/{INSTAGRAM_USERNAME}/", help=f"Opens @{INSTAGRAM_USERNAME}'s stories on Instagram in a new tab.")
                st.metric("Followers", format_metric(latest_data.get('Followers')))
                st.metric("Following", format_metric(latest_data.get('Following')))
                st.metric("Posts", format_metric(latest_data.get('Posts')))

            with col2:
                st.markdown("##### Website SEO Basics")
                st.metric("Robots.txt Found?", "Yes" if latest_data.get("Robots.txt Exists", False) else "No")
                sitemap_status = latest_data.get("Sitemap Found", "N/A")
                sitemap_display = "N/A"
                if pd.notna(sitemap_status) and isinstance(sitemap_status, str):
                    if "http" in sitemap_status or ".xml" in sitemap_status or ".php" in sitemap_status: sitemap_display = "✅ Found"
                    elif "Not found" in sitemap_status: sitemap_display = "❌ Not Found"
                    elif "Directive not found" in sitemap_status: sitemap_display = "⚠️ Not in robots.txt"
                    else: sitemap_display = f"❓ ({sitemap_status})" # Display the string if unknown format
                elif pd.isna(sitemap_status): sitemap_display = "N/A"
                st.metric("Sitemap Status", sitemap_display)


            st.subheader("📄 Latest SEO Details")
            st.write(f"**Title:** {latest_data.get('Title', 'N/A')}")
            with st.expander("Meta Description"):
                st.write(latest_data.get('Meta Description', 'N/A'))
            with st.expander("Sitemap Location/Status String"):
                st.write(f"`{latest_data.get('Sitemap Found', 'N/A')}`")

            st.write(f"**H1 Tags Found:**")
            # H1 tags are now stored as strings representing lists
            h1s_raw = latest_data.get('H1 Tags', pd.NA)
            h1_list = []
            if pd.notna(h1s_raw) and isinstance(h1s_raw, str) and h1s_raw.startswith('[') and h1s_raw.endswith(']'):
                try:
                    import ast
                    h1_list = ast.literal_eval(h1s_raw) # Safely evaluate string representation of list
                except (ValueError, SyntaxError):
                    h1_list = [f"Error parsing H1 tags from stored string: {h1s_raw}"]
            elif pd.notna(h1s_raw) and isinstance(h1s_raw, str): # Handle case where it might be saved differently
                 h1_list = [h1s_raw]
            elif isinstance(h1s_raw, list): # Should not happen if saved correctly, but handle
                h1_list = h1s_raw

            if h1_list:
                st.code("\n".join([f"- {h1}" for h1 in h1_list]))
            elif pd.isna(h1s_raw) or h1s_raw == 'None' or h1s_raw == '[]': # Check common NA string representations
                st.markdown("- *None Found or Not Available*")
            else: # Catch unexpected formats
                 st.markdown(f"- *Data: {h1s_raw}*")


            st.divider()
            st.subheader("📉 Growth Over Time")

            plot_df = history_df.copy()
            # Ensure Timestamp is datetime FOR PLOTTING
            plot_df['Timestamp'] = pd.to_datetime(plot_df['Timestamp'], errors='coerce')
            plot_df = plot_df.dropna(subset=['Timestamp'])

            # Plotting function (Unchanged logic)
            def plot_trend(df, y_col, title, y_label):
                if y_col not in df.columns:
                    st.warning(f"Column '{y_col}' not found in data for plotting.")
                    return

                df_plot = df.dropna(subset=['Timestamp', y_col]).copy()
                # Ensure Y col is numeric for plotting
                df_plot[y_col] = pd.to_numeric(df_plot[y_col], errors='coerce')
                df_plot = df_plot.dropna(subset=[y_col]) # Drop again after coercion

                if len(df_plot) > 1:
                    fig = px.line(df_plot, x='Timestamp', y=y_col, title=title, markers=True, labels={'Timestamp': 'Date', y_col: y_label})
                    fig.update_layout(xaxis_title='Date', yaxis_title=y_label)
                    st.plotly_chart(fig, use_container_width=True)
                elif len(df_plot) == 1:
                     st.write(f"Only one data point available for {title}. Need > 1 to plot trends.")
                else:
                    st.write(f"Not enough valid data points to plot {title}.")

            if len(plot_df) > 1:
                st.markdown("##### Instagram Trends")
                plot_trend(plot_df, 'Followers', 'Instagram Follower Growth', 'Followers')
                plot_trend(plot_df, 'Posts', 'Instagram Post Count Growth', 'Number of Posts')
            else:
                st.info("Need at least two data points with valid timestamps to plot growth trends. Fetch data again later.")

            st.divider()
            st.subheader("📜 Raw Data History")
            # Show most recent first, ensure Timestamp is first column
            display_columns = ['Timestamp'] + [col for col in ALL_COLUMNS if col != 'Timestamp']
            display_df = history_df[display_columns].copy()
            # Format Timestamp for display AFTER sorting/plotting
            display_df['Timestamp'] = pd.to_datetime(display_df['Timestamp'], errors='coerce').dt.strftime('%Y-%m-%d %H:%M:%S')
            st.dataframe(display_df.head(50)) # Show recent history


# --- Footer / Info (Updated) ---
st.sidebar.header("About")
st.sidebar.info(
    "This app provides a Social Media Growth Plan tailored for Chapter Hostels and a basic dashboard monitoring website SEO elements and Instagram stats. "
    "Monitoring data is fetched on demand and stored persistently in a Google Sheet. "
    "Instagram data fetching can sometimes be unreliable."
)
st.sidebar.header("Configuration")
st.sidebar.markdown(f"**Target URL:** `{TARGET_URL}`")
st.sidebar.markdown(f"**Instagram Handle:** `@{INSTAGRAM_USERNAME}`")
# st.sidebar.markdown(f"**Data File:** `{DATA_FILE}`") # <-- Remove CSV reference
st.sidebar.markdown(f"**Data Storage:** Google Sheet (`{WORKSHEET_NAME}` tab)") # <-- Update info
