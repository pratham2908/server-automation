---
description: Deploy local changes to production server
---

// turbo-all

1. Stage and commit local changes with a descriptive message
```bash
git add .
# Note: Always use a descriptive commit message based on the actual changes
git commit -m "update: deployment"
```

2. Push changes to GitHub
```bash
git push origin main
```

3. SSH into production server, pull code, install requirements, and restart service
```bash
ssh -i ssh-key-2.key ubuntu@68.233.115.135 "cd ~/automation-server && git pull origin main && ./venv/bin/pip install -r requirements.txt && sudo systemctl restart automation-server"
```
