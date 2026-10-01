# Cập nhật code CT Wallet trên server Linux

Tài liệu này dùng khi server đã được cấu hình lần đầu và đang chạy:

- source tại `/opt/ct-wallet-alpha-engine`;
- virtual environment Python tại `/opt/ct-wallet-venv`;
- service systemd tên `ct-wallet`;
- Nginx public ở cổng `80`;
- ứng dụng Python nội bộ ở `127.0.0.1:8081`;
- secrets trong `/etc/ct-wallet/ct-wallet.env`.

Các lệnh trong phần **server** được chạy bằng tài khoản có quyền `root`. Các lệnh
trong phần **local** được chạy trên máy chứa source code. Nếu service hoặc đường
dẫn của bạn khác, thay đúng giá trị trước khi chạy.

## 1. Nguyên tắc cập nhật

`data/wallet_portfolio_pnl.sqlite3` là database runtime và có thể rất lớn. Khi cập
nhật code, không đồng bộ đè thư mục `data/` từ máy local lên server. Database,
tracked wallets, label export và các snapshot hiện có trên server phải được giữ
nguyên.

Không copy `.env` từ máy local lên server. Service đọc secrets từ:

```text
/etc/ct-wallet/ct-wallet.env
```

Việc cập nhật nên theo thứ tự:

1. Kiểm tra và test code ở local.
2. Dừng service để không ghi database trong lúc backup/đồng bộ.
3. Backup database, file môi trường và cấu hình Nginx.
4. Đồng bộ code, không đụng vào `data/`, `.env` hoặc `node_modules`.
5. Cài dependency nếu `requirements.txt` hoặc `.tools/package-lock.json` thay đổi.
6. Khởi động service và chạy smoke test.
7. Chỉ chạy backfill/compact khi bản cập nhật thực sự yêu cầu.

## 2. Kiểm tra ở máy local

Từ máy local, vào đúng thư mục project:

```bash
cd "/Users/mac/Documents/Job task/CT-wallet/ct-wallet-alpha-engine"
```

Kiểm tra thay đổi và chạy test:

```bash
# Nếu source được quản lý bởi Git thì xem thay đổi; nếu không thì bỏ qua.
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git status --short
fi
python3 -m pytest -q -p no:cacheprovider
```

Nếu thư mục local không phải Git repository thì bỏ qua dòng `git status`. Nếu
project local không có `pytest` hoặc môi trường Python khác với server, vẫn cần
chạy ít nhất các test hiện có trong môi trường phát triển trước khi deploy.
Không đưa file chứa API key/private key vào commit hoặc lệnh `rsync`.

## 3. Khai báo biến dùng cho các lệnh local

Thay `SERVER_IP` bằng IP thật của server. Vì đường dẫn local có khoảng trắng, giữ
nguyên dấu ngoặc kép:

```bash
export CT_WALLET_LOCAL='/Users/mac/Documents/Job task/CT-wallet/ct-wallet-alpha-engine'
export CT_WALLET_SERVER='root@SERVER_IP'
export CT_WALLET_REMOTE='/opt/ct-wallet-alpha-engine'
```

Nếu deploy bằng user riêng thay vì `root`, đổi `CT_WALLET_SERVER` thành dạng:

```bash
export CT_WALLET_SERVER='deploy-user@SERVER_IP'
```

User đó phải có quyền ghi vào thư mục deploy hoặc cần dùng bước `chown` ở phần
server sau khi đồng bộ.

## 4. Kiểm tra server trước khi cập nhật

Đăng nhập server:

```bash
ssh root@SERVER_IP
```

Kiểm tra app, Nginx, dung lượng và database:

```bash
systemctl status ct-wallet --no-pager
systemctl status nginx --no-pager
df -h /opt
ls -lh /opt/ct-wallet-alpha-engine/data/wallet_portfolio_pnl.sqlite3
```

Nếu service đang chạy, dừng nó trước khi backup database:

```bash
systemctl stop ct-wallet
systemctl is-active ct-wallet
```

Kết quả cuối của lệnh `is-active` nên là `inactive`. Nginx có thể tiếp tục chạy,
nhưng trong khoảng thời gian cập nhật trang sẽ tạm thời trả `502` vì app backend
đang dừng.

## 5. Tạo backup trên server

Mỗi lần update tạo một thư mục backup có timestamp. Kiểm tra còn đủ dung lượng
trước khi copy database:

```bash
export CT_WALLET_BACKUP="/opt/ct-wallet-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$CT_WALLET_BACKUP/code" "$CT_WALLET_BACKUP/config"
df -h /opt
```

Backup code hiện tại (không bao gồm database và `node_modules`):

```bash
rsync -a \
  --exclude='data/' \
  --exclude='.tools/node_modules/' \
  --exclude='.env' \
  /opt/ct-wallet-alpha-engine/ \
  "$CT_WALLET_BACKUP/code/"
```

Backup database, environment và cấu hình Nginx:

```bash
cp -a \
  /opt/ct-wallet-alpha-engine/data/wallet_portfolio_pnl.sqlite3 \
  "$CT_WALLET_BACKUP/"

# Nếu còn SQLite WAL/SHM sau khi dừng service, backup chúng cùng database.
for f in /opt/ct-wallet-alpha-engine/data/wallet_portfolio_pnl.sqlite3-*; do
  [ -e "$f" ] && cp -a "$f" "$CT_WALLET_BACKUP/"
done

cp -a /etc/ct-wallet/ct-wallet.env "$CT_WALLET_BACKUP/config/ct-wallet.env"
cp -a /etc/nginx/sites-available/ct-wallet "$CT_WALLET_BACKUP/config/ct-wallet.nginx"
```

Nếu máy có `sqlite3`, kiểm tra database trước và sau backup:

```bash
sqlite3 /opt/ct-wallet-alpha-engine/data/wallet_portfolio_pnl.sqlite3 \
  'PRAGMA integrity_check;'
```

Kết quả hợp lệ là:

```text
ok
```

Giữ lại tên thư mục trong `CT_WALLET_BACKUP`; cần nó khi rollback.

## 6. Xem trước và đồng bộ code từ local

Thoát SSH hoặc mở terminal local khác, sau đó chạy dry-run để xem file sẽ thay
đổi. Lệnh này **không** copy dữ liệu thật:

```bash
rsync -aiv --dry-run \
  --exclude='.env' \
  --exclude='data/' \
  --exclude='.tools/node_modules/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  "$CT_WALLET_LOCAL/" \
  "$CT_WALLET_SERVER:$CT_WALLET_REMOTE/"
```

Đặc biệt kiểm tra dry-run không có các file sau trong danh sách ghi đè:

- `data/wallet_portfolio_pnl.sqlite3`;
- `data/*.sqlite3-wal` hoặc `data/*.sqlite3-shm`;
- `.env`;
- `.tools/node_modules/`.

Nếu danh sách đúng, chạy đồng bộ thật:

```bash
rsync -aiv \
  --exclude='.env' \
  --exclude='data/' \
  --exclude='.tools/node_modules/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  "$CT_WALLET_LOCAL/" \
  "$CT_WALLET_SERVER:$CT_WALLET_REMOTE/"
```

Không dùng `--delete` ở lần đầu nếu chưa chắc server không có file local cần giữ.
Nếu source đã được quản lý chặt bằng Git và muốn xoá các file code cũ đã bị xoá
ở local, có thể dùng `--delete-delay` ở một lần deploy riêng sau khi đã kiểm tra
dry-run kỹ; vẫn phải giữ các `--exclude` ở trên.

## 7. Cài dependency và sửa quyền trên server

Đăng nhập lại server:

```bash
ssh root@SERVER_IP
cd /opt/ct-wallet-alpha-engine
```

Đảm bảo quyền sở hữu runtime đúng:

```bash
chown -R ctwallet:ctwallet /opt/ct-wallet-alpha-engine
chmod 755 /opt/ct-wallet-alpha-engine/data
chmod 664 /opt/ct-wallet-alpha-engine/data/wallet_portfolio_pnl.sqlite3
chown root:ctwallet /etc/ct-wallet/ct-wallet.env
chmod 640 /etc/ct-wallet/ct-wallet.env
```

Nếu `requirements.txt` thay đổi, cài lại Python dependency trong virtual
environment:

```bash
/opt/ct-wallet-venv/bin/python -m pip install -r requirements.txt
```

Nếu `.tools/package.json` hoặc `.tools/package-lock.json` thay đổi, cài lại CLI:

```bash
cd /opt/ct-wallet-alpha-engine/.tools
npm ci --omit=dev
cd /opt/ct-wallet-alpha-engine
```

Không cần cài lại dependency nếu các file dependency không thay đổi.

## 8. Schema hoặc dữ liệu sau khi nâng cấp

Không chạy tất cả script backfill theo thói quen. Nhiều script có thể đọc/ghi
hàng triệu dòng hoặc gọi provider và làm tăng quota.

Chỉ chạy script mà release note của bản code yêu cầu. Ví dụ, nếu thay đổi schema
canonical yêu cầu backfill:

```bash
/opt/ct-wallet-venv/bin/python \
  /opt/ct-wallet-alpha-engine/scripts/backfill_canonical_wallet_data.py
```

Nếu cần compact/vacuum database, phải chắc chắn đã có backup và đã dừng service:

```bash
/opt/ct-wallet-venv/bin/python \
  /opt/ct-wallet-alpha-engine/scripts/compact_canonical_serving.py
```

Không chạy `--prune`, `--vacuum`, backfill provider hoặc refresh toàn bộ cohort
trong một lần update thông thường nếu không có yêu cầu rõ ràng.

## 9. Khởi động lại và kiểm tra

Nếu chỉ cập nhật Python/frontend, restart service:

```bash
systemctl restart ct-wallet
```

Chỉ cần `daemon-reload` nếu bạn đã thay đổi file unit systemd:

```bash
systemctl daemon-reload
systemctl restart ct-wallet
```

Nếu thay đổi cấu hình Nginx, kiểm tra trước rồi mới reload:

```bash
nginx -t
systemctl reload nginx
```

Kiểm tra trạng thái:

```bash
systemctl status ct-wallet --no-pager
systemctl status nginx --no-pager
systemctl is-active ct-wallet
systemctl is-enabled ct-wallet
```

Service ứng dụng phải là `active`; để tự chạy sau reboot phải là `enabled`.

Kiểm tra API nội bộ và qua Nginx:

```bash
curl -fsS http://127.0.0.1:8081/api/health
curl -fsS http://127.0.0.1/api/health
curl -I http://127.0.0.1/wallet_Intelligence/
```

Smoke test tối thiểu cần xác nhận:

- API health trả `status: ok`;
- `wallets_count` có giá trị hợp lý, thường là `192` với cohort hiện tại;
- trang `/wallet_Intelligence/` trả `200`;
- frontend tải được dữ liệu danh sách và mở được chi tiết một wallet;
- không có traceback hoặc lỗi import trong log.

Xem log nếu có lỗi:

```bash
journalctl -u ct-wallet -n 150 --no-pager
journalctl -u nginx -n 100 --no-pager
```

Theo dõi realtime trong lúc kiểm tra UI:

```bash
journalctl -u ct-wallet -f
```

## 10. Rollback khi bản update lỗi

Nếu API health lỗi hoặc frontend không tải được, dừng service trước:

```bash
systemctl stop ct-wallet
```

Đặt biến tới backup của lần deploy cần quay lại:

```bash
export CT_WALLET_BACKUP='/opt/ct-wallet-backups/YYYYMMDD-HHMMSS'
```

Khôi phục code, vẫn giữ database runtime hiện tại:

```bash
rsync -a --delete \
  --exclude='data/' \
  --exclude='.env' \
  --exclude='.tools/node_modules/' \
  "$CT_WALLET_BACKUP/code/" \
  /opt/ct-wallet-alpha-engine/
```

Chỉ khôi phục database nếu database bị hỏng hoặc bản update đã thay đổi dữ liệu
không thể đảo ngược. Đảm bảo service đang dừng rồi mới làm:

```bash
cp -a \
  "$CT_WALLET_BACKUP/wallet_portfolio_pnl.sqlite3" \
  /opt/ct-wallet-alpha-engine/data/wallet_portfolio_pnl.sqlite3

chown ctwallet:ctwallet \
  /opt/ct-wallet-alpha-engine/data/wallet_portfolio_pnl.sqlite3
chmod 664 \
  /opt/ct-wallet-alpha-engine/data/wallet_portfolio_pnl.sqlite3
```

Khởi động và kiểm tra lại:

```bash
systemctl start ct-wallet
systemctl status ct-wallet --no-pager
curl -fsS http://127.0.0.1:8081/api/health
```

## 11. Quy trình ngắn cho các lần update thông thường

Khi đã quen quy trình, các bước tối thiểu là:

```bash
# Local
cd "/Users/mac/Documents/Job task/CT-wallet/ct-wallet-alpha-engine"
python3 -m pytest -q -p no:cacheprovider
rsync -aiv --dry-run \
  --exclude='.env' --exclude='data/' \
  --exclude='.tools/node_modules/' \
  --exclude='__pycache__/' --exclude='.pytest_cache/' \
  ./ root@SERVER_IP:/opt/ct-wallet-alpha-engine/

# Sau khi kiểm tra dry-run, chạy lại bỏ --dry-run.

# Server
ssh root@SERVER_IP
systemctl stop ct-wallet
# Tạo backup theo phần 5.
# Cài dependency nếu có thay đổi.
systemctl start ct-wallet
systemctl status ct-wallet --no-pager
curl -fsS http://127.0.0.1:8081/api/health
curl -I http://127.0.0.1/wallet_Intelligence/
```

Không đóng terminal giữa chừng khi đang chạy `rsync`, backup database hoặc cài
dependency. Nếu kết nối SSH bị gián đoạn, kiểm tra `systemctl status` và checksum
file trước khi chạy lại, không giả định rằng lần trước đã hoàn tất.
