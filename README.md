# SoloQ Snitch

**SoloQ Snitch** is a feature-rich Discord bot designed for NA League of Legends players who want deeper insights into their own gameplay as well as the performance and statistics of other players. It connects directly to the Riot Games API to track stats, display leaderboards, analyze match data, and send automatic alerts for win or loss streaks all within your own personal Discord server.

---

## Commands Overview

### 👥 Player Tracking
| Command | Description |
|----------|-------------|
| `/add` | Add a player to the tracking list |
| `/remove` | Remove a player from the tracking list |
| `/leaderboard` | View your server’s ranked leaderboard |
| `/strongest` | Display an image showing the current strongest player |
| `/rank` | Check a player’s current rank |
| `/lastplayed` | See when a player last played in each mode |
| `/link` | Link a Discord account to a Riot ID |
| `/unlink` | Unlink a Discord account from a Riot ID |
| `/clear` | Clear and unlink all tracked players |

### 📊 Performance & Analysis
| Command | Description |
|----------|-------------|
| `/stats` | View a player’s full performance breakdown |
| `/history` | Show recent match history |
| `/feederscore` | Calculate a player’s feeder score |
| `/rolesummary` | Display a player’s role distribution chart |
| `/firstblood` | Show first-blood statistics |
| `/arenagod` | Show “Adapt to All Situations” Arena challenge stats |

### 🧠 Miscellaneous
| Command | Description |
|----------|-------------|
| `/mastery` | View champion mastery for a player |
| `/lfg` | Notify other linked players you’re looking for a game |
`/help` | Displays a list of all commands |

### ⚙️ Bot Settings
| Command | Description |
|----------|-------------|
| `/tiltcheck` | Toggle alerts for losing streaks |
| `/wincheck` | Toggle alerts for win streaks |
| `/setchannel` | Set the notification channel for alerts |

---

## Setup Instructions

### 1. **Clone the Repository**
Clone the repository to your local machine:
```bash
git clone https://github.com/<your-username>/SoloQ-Snitch.git
cd SoloQ-Snitch
```
### 2. Install dependencies
```bash 
pip install -r requirements.txt
```

### 3. Environment Setup
Create a .env file in your project's root directory
```bash
RIOT_API_KEY = your_riot_api_key
DISCORD_TOKEN = your_discord_token
```

### 4. Running the bot
```bash
python LeagueBot.py
```
---
## Questions, Suggestions & Bug Reports
 If you have a question, want to suggest a new feature, or discover a bug, feel free to reach out through starting a discussion or opening an issue.


 ## License
Distributed under the MIT License. See the [LICENSE](LICENSE) file for more information.



