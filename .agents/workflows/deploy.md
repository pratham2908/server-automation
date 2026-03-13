---
description: Deploy local changes to production server
---

// turbo-all

1. Add and commit all local changes

```bash
git add .
git commit -m "update"
```

2. Push changes to GitHub

```bash
git push origin main
```

3. Deploy to production server via SSH

```bash
ssh -i ssh-key-2.key ubuntu@68.233.115.135 "cd automation-server && git pull && sudo systemctl restart automation-server"
```
