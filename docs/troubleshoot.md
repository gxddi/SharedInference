
### SSH script
```bash
sudo resolvectl dns wlp3s0 '1.1.1.1#cloudflare-dns.com'
sudo resolvectl dnsovertls wlp3s0 yes
sudo resolvectl domain wlp3s0 '~.'
sudo systemctl restart tailscaled
sudo tailscale up
```

### Vultr GPU instance
ssh node1@207.148.17.19
pass: 7wE_mnxL!n+v4{ik

### Vultr CPU host
ssh host@66.42.90.154
