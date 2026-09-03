	discord-music-bot

1. Setup in the Discord Developer Portal
2.Go to the Discord Developer Portal.
3.Create a new application (New Application) and add a bot in the Bot menu.
4.Grant the bot administrative rights in the Privileged Gateway Intents section.
5.Copy the bot’s token.
6.Invite the bot to the server via the OAuth2 URL Generator (select scopes: bot + permissions: Administrator).
7.Open the file you downloaded, then open the bot.py file using PyCharm.
8.On line 22, replace the text with your own token.
9.Open the terminal and install the required libraries using the commands:
10.pip install discord.py / pip install yt-dlp / pip install aiohttp.
11.Download FFmpeg from this link:https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-2026-08-30-git-818cecc6e1-essentials_build.7z .
12.Extract the .7z archive to the C drive.
13.Press the Windows key on your keyboard and type “Edit the system environment variables”.
14.Click the “Environment Variables” button.
15.In the System variables section, find the PATH variable.
16.Double‑click it, then click the “New” button and paste this text: C:\ffmpeg-2026-08-30-git-818cecc6e1-essentials_build\bin, then click OK.
