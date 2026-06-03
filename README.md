SCRIPT DOES NOT WORK, it is still in development.

1. put your exact resume titles in config.yaml, also change your requirements for the script to apply. Here you can add or remove your resumes for the script to see. 
2. create /resumes, and add txt files with contents of your resumes for AI to analyze.
3. create .env, and write down your GEMINI_API_KEY and OPENROUTER_API_KEY
4. Create applie.json containing "[]"
5. execute auth_setup.pyfrom the device where u usually use HH( laptop / desktop ), repostitory has shell.nix for conviniance of doing so on NixOS. Created file state.json move to jobFinder directory.
6. docker compose up -d --build
7. for making it a part of another docker-compose.yaml add relative path to the rep in jobFinder/docker-compose.yaml ( build: ), and change volume paths to the relative ones.
