# CT Wallet Alpha Engine

Quy trình cập nhật code lên server Linux: [DEPLOY_UPDATE.md](DEPLOY_UPDATE.md).

Pipeline gán nhãn ví Solana hoàn toàn deterministic:

`wallet address -> GMGN raw data -> normalize -> statistical features -> rank S/A/B/C/D + clan priority -> CSV + JSON`

Không có LLM/AI trong bước quyết định label. Mỗi kết quả lưu `formula_version` và
`input_hash` trong SQLite để có thể audit và tái lập.

## Nhãn nhóm G1--G4

`app/portfolio_pnl/group_labels.py` là engine độc lập cho bốn nhóm nghiệp vụ. Nó
chuẩn hoá một quan sát cho mỗi token từ holdings và activity, ưu tiên timestamp/
size/order đầu tiên trong activity, rồi mới dùng trường holdings làm fallback.
Thiếu market-cap, timestamp hoặc capital base không được thay bằng số 0 để làm
đạt điều kiện.

- **G1**: tối thiểu 15 token và ít nhất 60% vừa vào ở market cap `$30K--$150K`
  vừa có tuổi entry không quá 60 phút.
- **G2**: median volume tối thiểu `$25K`, hoặc median volume `$5K` cùng median
  buy order `$1K` và capital base tối thiểu `$100K`; thêm ít nhất 3/5 điều kiện
  conviction (multi-buy `>=60%`, giữ `>=12h`, first sell `<90%`, bán sạch `<50%`,
  averaging down `<20%`).
- **G3**: ít nhất hai CA cùng keyword narrative trong cửa sổ mua 72 giờ.
- **G4**: realized PnL lớn nhất trên một token tối thiểu `$100K` và đạt ít nhất
  2/3 điểm holder (median span `>=12h`, median first sell `<90%`, sell-100% rate
  `<50%`).

Kết quả nằm trong `group_labels` của report, `metrics.group_features` của output
API và các cột `Group_Labels`, `G1_Early_Lowcap_Rate_Pct`, `G2_Whale`,
`G2_Conviction`, `G4_Max_Realized_PnL_USD` của CSV. Formula version hiện tại là
`wallet_labels_v6_23_weighted_rank_9_metrics_audit_no_inactive_veto` để các label cache cũ tự động được tính lại. Một wallet có thể thuộc nhiều group nếu đạt nhiều điều kiện; `labels` là danh sách group chuẩn duy nhất và `group_count` là số lượng tương ứng.

## Xuất label cho một ví

Khi database đã có snapshot, chạy:

```bash
python3 scripts/label_one_wallet.py <WALLET_ADDRESS> --name "Tên ví"
```

Kết quả mặc định được upsert theo địa chỉ ví vào:

- `data/single_wallet_label.json`
- `data/single_wallet_label.csv`
- `data/single_wallet_summary.json`
- `data/single_wallet_summary.csv`

Để xuất **toàn bộ dữ liệu một wallet** từ SQLite mà không gọi provider, gồm raw
dashboard/holdings, activity events, intraday events, external snapshots,
normalized token observations, full analysis report, projected label và trạng
thái refresh:

```bash
python3 scripts/export_full_wallet.py <WALLET_ADDRESS> \
  --output /path/to/wallet_full_export.json
```

File này có schema `wallet_full_export_v1`. Đây là bản export đầy đủ để audit/
backup; file label JSON/CSV thông thường chỉ là output đã chiếu gọn cho dashboard.
Export đầy đủ cũng bao gồm các raw pages, token rows, crawl runs và lịch sử
feature/label runs của wallet; dữ liệu cache on-chain dùng chung toàn database
không được nhân bản vào từng file wallet.

## Canonical data cho toàn bộ cohort

Để phục vụ 192 wallet liên tục, database có thêm serving layer canonical:

- `canonical_wallet_state`: một dòng/wallet, lưu coverage, số token/event,
  component timestamps, feature/formula version và trạng thái `READY/PARTIAL`.
- `canonical_token_facts`: token facts dạng typed columns cho các metric G1--G4;
  không cần giải nén JSON khi lọc/sort.
- `canonical_features_current`: feature report mới nhất theo `input_hash`.
- `canonical_labels_current`: label projected mới nhất theo `input_hash`.
- `canonical_label_inputs_current`: **một dòng đầy đủ cho mỗi wallet**, có
  address/name/emoji, 23 trường legacy trong file rule, toàn bộ trường tổng hợp
  G1--G4, `group_labels_json`, nhãn slug/text legacy và `value_sources_json`.
  Đây là bảng đầu vào ổn định cho job label; giá trị không có bằng chứng vẫn là
  `NULL`, không bị đổi thành 0. Các trường token chi tiết nằm trong
  `canonical_token_facts` (một dòng/wallet/token).
- `wallet_events`: event buy/sell/transfer đã deduplicate theo
  `(wallet,event_id)` và lưu các cột typed; raw event đầy đủ vẫn nằm trong
  `raw_pages` để audit.

Các cột legacy được giữ nguyên tên rule: `money_earned_primary_usd`,
`capital_base_usd`, `current_portfolio_usd`, `roi_on_trading_cost_pct`,
`pnl_over_deposits_pct`, `jackpot_dependency_pct`, `portfolio_max_drawdown_pct`,
`max_realized_pnl_drawdown_usd`, `median_volume_last_30_tokens_usd`,
`median_buy_order_last_30_tokens_usd`, `longest_loss_streak_tokens`,
`current_loss_streak_tokens`, `withdrawal_count`, `total_withdrawals_usd`,
`median_outflow_holding_pct`, `median_transfer_holding_pct`, `sell_100pct_rate`,
`transfer_100pct_rate`, `multi_buy_token_rate_pct`,
`median_position_action_span_hours`, `median_first_sell_pct`,
`averaging_down_token_rate_pct` và `max_realized_pnl_single_token_usd`.

Các cột G1--G4 gồm `lowcap_entry_count`, `lowcap_entry_rate_pct`,
`early_entry_count`, `early_entry_rate_pct`, `early_lowcap_count`,
`early_lowcap_rate_pct`, `median_entry_market_cap_usd`,
`median_entry_age_minutes`, `token_sample`, `median_volume`,
`median_buy_order`, `whale`, `multi_buy`, `holding`, `first_sell`,
`sell_100pct_conviction`, `averaging_down`, `conviction`,
`conviction_pass_count`, `same_theme_ca_count`, `same_theme_ca_share_pct`,
`largest_ca_share_in_theme`, `theme_buy_window_hours`, `is_basket_narrative`
và `holder_score`. Trong `canonical_token_facts`, các tên rule token như
`token_launch_time`, `wallet_first_buy_time`, `wallet_first_buy_price`,
`market_cap_at_first_buy_usd`, `first_buy_amount_usd`, `total_position_usd`,
`first_buy_timestamp`, `buy_usd`, `token_name` và `symbol` được trả ra cùng
các cột typed nội bộ tương ứng.

Chạy backfill toàn bộ cohort sau khi nâng cấp schema:

```bash
python3 scripts/backfill_canonical_wallet_data.py
```

Backfill hiện đã hoàn tất cho 192/192 wallet. Continuous refresh chỉ chọn wallet
có ít nhất một component thiếu hoặc đã quá TTL; wallet còn mới không gọi provider
và không chạy lại analysis. Sau khi provider trả dữ liệu, analysis và canonical
upsert chỉ chạy nếu nội dung normalized thực sự thay đổi. Nếu payload giống lần
trước, watermark được cập nhật để giữ freshness nhưng dashboard blob, event rows,
token facts và label không bị ghi lại. Nếu refresh một component lỗi, canonical
layer giữ label cuối cùng và đánh dấu manifest là `PARTIAL` thay vì xoá dữ liệu
hợp lệ.

Event cũ đã được chuyển từ JSON payload lặp sang typed columns bằng:

```bash
python3 scripts/compact_canonical_events.py
```

Raw pages không bị xoá trong bước này.

Serving layer có thể compact riêng sau các lần nâng cấp schema:

```bash
python3 scripts/compact_canonical_serving.py
```

Lệnh này xoá các bản sao `raw_fields_json` và token observations lồng trong
current feature payload; token facts typed và raw pages vẫn được giữ nguyên.

Canonical data có thể đọc trực tiếp qua API:

```http
GET /api/pnl/canonical
GET /api/pnl/canonical/{wallet_address}?include_tokens=true
GET /api/pnl/cohort-labels
```

Endpoint đầu trả coverage/status của cả cohort; endpoint thứ hai trả state,
`label_inputs_current`, current feature, current label và token facts typed của
một wallet. Endpoint `cohort-labels` là read model cho dashboard: một request
trả identity (địa chỉ, tên, emoji), toàn bộ 48 feature label input, group G1--G4,
legacy/projected labels và trạng thái refresh của từng component cho toàn bộ
cohort. Frontend poll endpoint này mỗi 60 giây, còn raw token facts vẫn chỉ tải
khi cần xem snapshot một wallet. `wallet_full_export.py` cũng ghi nguyên bảng
label input này vào phần `canonical`.

Raw data không bị chuẩn hoá đè hoặc xoá: `raw_pages`, raw snapshots và lịch sử
crawl vẫn là bản audit bất biến. Khi refresh thành công, canonical row được
upsert theo `input_hash`; khi một component lỗi, label input và label gần nhất
được giữ lại, còn `data_status` chuyển sang `PARTIAL/ERROR` để downstream không
nhầm dữ liệu cũ là dữ liệu mới.

Để tạo hai file nhẹ chỉ chứa label input của toàn bộ cohort (wallet-level, không
nhân bản token observations), chạy:

```bash
python3 scripts/export_canonical_label_inputs.py
```

Kết quả mặc định là `data/canonical_label_inputs_192.json` và
`data/canonical_label_inputs_192.csv`. Token-level facts cho G1/G3 được giữ ở
`canonical_token_facts` và trong phần `canonical.token_facts` của full export.

Chạy lại cùng địa chỉ sẽ thay bản ghi cũ, không tạo duplicate. Có thể đổi đích bằng
`--output /path/to/labels.json`; file CSV cùng tên được tạo tự động.

Pipeline mặc định chỉ đọc snapshot hiện có trong SQLite, không phụ thuộc GMGN tại
thời điểm xuất label:

```bash
python3 scripts/label_one_wallet.py <WALLET_ADDRESS> \
  --name "Tên ví" \
  --output data/single_wallet_label.json
```

Luồng này kiểm tra completeness, derive các feature rồi xuất label. Nếu dữ liệu
chưa đủ, kết quả được đánh dấu `PROVISIONAL` thay vì suy đoán.

Output gồm detailed JSON/CSV, summary JSON/CSV, `data_availability`, cache input
hash theo toàn bộ nội dung và ngày UTC, cùng `conviction_v2` tách riêng kết quả
size edge khỏi hành vi scale-in.

Chỉ khi cần cập nhật đồng bộ trước khi tính mới dùng:

```bash
python3 scripts/label_one_wallet.py <WALLET_ADDRESS> --refresh
```

Lệnh `--refresh` cần `GMGN_API_KEY` và `GMGN_PRIVATE_KEY` trong `.env`.

Dashboard dùng cách an toàn hơn: trả label từ DB ngay và xếp việc cập nhật provider
vào hàng đợi nền; lỗi HTTP 429 không làm hỏng label đang có.

## Lưu trữ

`data/wallet_portfolio_pnl.sqlite3` là nguồn dữ liệu runtime duy nhất. Payload JSON
lớn được nén trong SQLite nhưng vẫn giải nén trong suốt khi đọc. Không sửa trực
tiếp database; dùng collector/background refresh để cập nhật và dùng các script
export để tạo JSON/CSV.

Nếu database cũ còn lưu payload dạng JSON text, có thể nén và thu hồi dung lượng:

```bash
python3 scripts/compact_database.py --vacuum
```

Raw pages vẫn được giữ để audit trong 90 ngày đối với run thành công và 14 ngày
đối với run lỗi. Có thể xem trước số dòng đủ điều kiện xóa bằng
`PnlStore.prune_raw_pages(..., dry_run=True)`; khi đã kiểm tra, chạy lệnh dọn dẹp
rõ ràng:

```bash
python3 scripts/compact_database.py --prune \
  --prune-raw-days 90 --prune-failed-days 14 --vacuum
```

Các event activity được upsert theo `event_id` ổn định nên đọc lại trang biên
không tạo duplicate. Bảng `wallet_component_watermarks` lưu cursor, khoảng thời
gian phủ và hash payload của từng component; bảng `wallet_refresh_queue` giữ yêu
cầu refresh thủ công ở mức ưu tiên cao hơn vòng round-robin.

## Dashboard/API

```bash
python3 run_server.py
```

Dashboard mặc định chạy tại `http://127.0.0.1:8081`. Có thể đổi cổng bằng biến
`CT_WALLET_PORT`.

Nhập địa chỉ rồi chọn **Xuất label từ DB** để tính ngay trên snapshot SQLite hiện
có, hoặc **Xem snapshot** để mở dữ liệu PnL. Sau khi xuất label, server tự xếp một
job cập nhật holdings/activity vào database ở nền; job này không tính lại hoặc
thay đổi label vừa trả về. Trang hiển thị Rank, Performance
score, Confidence, PnL, Growth, Cluster WR, Conviction, Clan/Priority, checklist
độ đầy đủ dữ liệu và cho tải label JSON/summary CSV. Snapshot thiếu hoặc quá 48
giờ được đánh dấu rõ là `PROVISIONAL`.

`POST /api/pnl/analyze-and-label` không gọi provider trong request nữa. Trạng thái
job cập nhật database xem tại `GET /api/pnl/refresh/status/{wallet_address}`.

Server cũng tự chạy cập nhật liên tục theo round-robin. Mặc định mỗi 2 phút kiểm
tra tối đa 4 wallet đến hạn và nghỉ 5 giây giữa các wallet; wallet đủ mới bị bỏ
qua nên vòng quét không tạo request GMGN thừa. Mỗi component có TTL riêng:
activity/profit cập nhật thường xuyên, holdings đầy đủ cập nhật thưa hơn để giảm
pagination và rate limit. Tiến độ và lỗi gần nhất xem tại:

```http
GET /api/pnl/auto-refresh/status
```

Có thể cấu hình bằng `.env` hoặc environment:

```text
CT_WALLET_AUTO_REFRESH=1
CT_WALLET_REFRESH_BATCH_SIZE=4
CT_WALLET_REFRESH_INTERVAL_SECONDS=120
CT_WALLET_REFRESH_WALLET_DELAY_SECONDS=5
CT_WALLET_REFRESH_INITIAL_DELAY_SECONDS=60
CT_WALLET_TTL_HOLDINGS_SECONDS=21600
CT_WALLET_TTL_PROFITS_SECONDS=900
CT_WALLET_TTL_CALENDAR_SECONDS=900
CT_WALLET_TTL_STATS_SECONDS=1800
CT_WALLET_TTL_ACTIVITY_SECONDS=600
CT_WALLET_TTL_INTRADAY_SECONDS=600
```

Lỗi provider được lưu trong bảng `wallet_refresh_state` và retry theo exponential
backoff. Startup backfill, refresh thủ công và refresh định kỳ dùng chung một worker
để không gửi request GMGN đồng thời. Job nền chỉ cập nhật database, không tự thay
label vừa xuất.

Refresh thủ công được ghi vào hàng đợi với độ ưu tiên cao nên được xử lý trước vòng
round-robin tiếp theo và được phép bypass TTL của các component. Mỗi component có
watermark riêng; lỗi ở một component không ghi đè snapshot hợp lệ của component
khác. Activity được lưu thành event idempotent để các lần refresh overlap không làm
tăng số dòng trùng; overlap chỉ upsert phần event mới, không quét lại toàn bộ lịch
sử.

Mỗi wallet được theo dõi riêng theo các component `holdings`, `profits`, `calendar`,
`stats`, `activity`, `intraday`. Component thành công được commit ngay; nếu một phần
bị HTTP 429 hoặc lỗi mạng, lần sau chỉ retry phần còn thiếu/quá cũ. `intraday` được
dựng lại từ activity trong SQLite nên không gọi provider lần thứ hai. Trạng thái
chi tiết từng component có trong `GET /api/pnl/refresh/status/{wallet_address}`.
Các trang raw của cùng một request phân trang được ghi trong một transaction SQLite;
raw pages vẫn được giữ làm audit. Snapshot dashboard chỉ được ghi lại khi nội dung
component thật sự thay đổi. `canonical_token_facts` dùng `fact_hash` để tránh
UPDATE từng token khi typed facts không đổi, còn worker chỉ đọc manifest nhỏ sau
analysis thay vì tải lại toàn bộ token facts.

Khi server khởi động, background job kiểm tra `message.txt` và chỉ gọi GMGN cho
ví chưa có snapshot hoặc snapshot thiếu `summary`, `rows` hay `daily_calendar`.
Snapshot đã đủ trường không bị refresh lại.

Hai nút **Xuất JSON** và **Xuất CSV** tính lại `wallet_labels_v6_23_weighted_rank_9_metrics_audit_no_inactive_veto` từ SQLite hiện
tại, ghi đồng thời hai file rồi tải định dạng đã chọn. API:

```http
GET /api/pnl/backfill/status
GET /api/pnl/exports/download?format=json
GET /api/pnl/exports/download?format=csv
```

Nút refresh toàn bộ đã được bỏ để tránh crawl lại 192 ví và tốn quota/thời gian.

Startup enrichment cũng chỉ bổ sung phần thiếu: stats 7D/30D, intraday có thể
derive từ full activity và holdings rỗng chưa từng được xác nhận là crawl hoàn
chỉnh. Activity 365 ngày đã complete sẽ không bị tải lại.

Chạy thủ công cùng cơ chế resume-safe:

```bash
python3 scripts/backfill_missing_data.py --export
```

Mở rộng activity history phải được yêu cầu rõ để tránh tải lại dữ liệu lớn:

```bash
python3 scripts/backfill_wallet_activity.py --days 1095 --export
```

`backfill_wallet_activity.py` dùng cursor ở trang biên của snapshot hiện tại,
merge + deduplicate events và ghi `extension_checkpoint` nếu một wallet vượt
`--max-pages`. Chạy lại cùng lệnh sẽ tiếp tục từ checkpoint, không tải lại phần
lịch sử mới hơn. Chỉ dùng `--restart` khi thật sự muốn bỏ cursor và crawl lại từ
trang đầu.

Để bổ sung nguồn funding và transfer history cho các wallet còn
`PARTIAL_CAPITAL_PROXY`, điền `SOLSCAN_API_KEY` rồi chạy:

```bash
python3 scripts/backfill_solscan_enrichment.py --max-pages 100
```

Script mặc định chỉ xử lý wallet thiếu lịch sử vốn. Nếu chạm `--max-pages`, nó
lưu `next_page` hoặc `next_cursor`; chạy lại đúng lệnh sẽ tiếp tục checkpoint. Dùng `--wallet` để
thử một ví, `--all-wallets` để mở rộng toàn bộ 192 ví, và chỉ dùng `--force` khi
muốn tải lại từ trang đầu. Snapshot Solscan được lưu riêng trong bảng
`external_data_snapshots`; dữ liệu `partial` chưa tham gia chấm rank.

Nếu API key không có gói Pro, collector tự chuyển sang endpoint Playground miễn
phí và lưu raw enhanced transactions bằng cursor. Endpoint `funded-by` yêu cầu
gói Solscan trả phí; khi không có quyền, thời điểm bắt đầu được lấy từ
`GMGN fund_from_ts` hoặc activity sớm nhất thay vì đoán nguồn funding.

Nếu Solscan Playground hết quota, có thể bổ sung riêng bằng chứng funding đầu
đời từ Solana JSON-RPC công khai. Script mặc định chỉ chọn wallet chưa có
`VERIFIED_EQUITY_HISTORY`, phân trang đến giao dịch cũ nhất và lưu nguồn/timestamp
vào `external_data_snapshots`:

```bash
python3 scripts/backfill_solana_funding.py --max-pages 20 --delay 1
```

Chỉ native SOL nhận trực tiếp hoặc transfer-in USD đã quan sát được mới được
dùng làm starting equity; wallet không đủ số tiền đầu kỳ vẫn giữ trạng thái
`PARTIAL_CAPITAL_PROXY`.

Để định giá native-SOL funding theo đúng ngày lịch sử, tải daily SOL/USD rồi
export lại label:

```bash
python3 scripts/backfill_sol_price_history.py
python3 scripts/export_wallet_labels_v6.py
```

Collector ưu tiên CoinGecko và tự dùng Binance Spot public market data cho
giai đoạn vượt quá cửa sổ lịch sử của CoinGecko Public API.

Nếu dữ liệu không đủ sample, hệ thống xếp rank C và giữ các label chi tiết ở trạng
thái `INSUFFICIENT_SAMPLE`; không suy đoán để lấp dữ liệu thiếu.

Schema công khai `wallet_rank_output_v6` xuất identity, labels, priority, metrics,
rank audit và danh sách metric còn thiếu. Rank dùng điểm có trọng số trên đúng 9
trường: money earned `18%`, ROI on trading cost `17%`, jackpot dependency `12%`,
realized PnL drawdown USD `13%`, longest loss streak `8%`, current loss streak
`12%`, median outflow holding `5%`, median first sell `7%`, averaging down `8%`.
Mỗi trường được chuẩn hóa về 0–100 theo các mốc cố định; tổng điểm là trung bình
trọng số trên các trường có dữ liệu. `rank_confidence` bằng tỷ lệ trọng số đã có
dữ liệu, và các trường thiếu được ghi trong `missing_rank_metrics`.

- S: điểm `>=85` và confidence `>=80%`.
- A: điểm `>=70`.
- B: điểm `>=55`.
- C: điểm thấp hơn `55`, coverage trọng số dưới `60%`, hoặc thiếu activity.
- D: veto cứng khi PnL dưới `-$100K`, current loss streak `>=8`, severe realized
  drawdown `>=90%` trong trạng thái PnL không dương, hoặc
  averaging down `>=50%` đi cùng loss streak `>=6`.

Các veto D được xét trước điểm tổng; `overall_rank_score`, từng contribution và
veto đều có trong `metrics`/`rank_audit` để kiểm tra. Không dùng trường legacy
`max_drawdown_to_peak_pct` làm rank. Thiếu activity đưa ví vào C provisional thay vì
suy đoán. Activity/inactivity vẫn được xuất trong metrics để tham khảo nhưng không
còn là veto D. Toàn bộ ngưỡng nằm trong `app/portfolio_pnl/rank_engine.py`.

Clan engine chạy độc lập với rank. Nó chỉ phát tín hiệu khi nhiều wallet mua cùng
token trong cửa sổ entry 14 ngày: 3–4 là `CLAN_WATCH`, từ 5 là `CLAN_ALERT`, và
từ 5 wallet có ít nhất 2 wallet S/A là `PRIORITY_ALERT`. Dữ liệu hiện chưa có
equity history, external cash flow hay buy sequence; các metric này luôn để null
và được liệt kê trong `data_availability.missing_metrics`.
