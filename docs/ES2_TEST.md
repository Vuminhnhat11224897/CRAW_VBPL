# Chuẩn bị test ghi v2 vào cụm 23

Luồng đã tích hợp kiểm tra OCR/số hiệu/ngày vào `build.prepare`, trước LLM, embedding, quan hệ và publish. Đích là ba index v2 trên cụm 23 qua Tailscale `100.77.4.35:9200` (IP LAN `10.1.10.23:9200`): `legal-docs`, `legal-chunk-section-v2`, `legal-changed-v2`. Export mặc định chỉ dùng mapping/đối chiếu v2, không cần các index v3.

## Bộ dữ liệu đã chuẩn bị

Thư mục làm việc `data_es2_test/input` có SQLite riêng và bản sao đã kiểm SHA-256 của bốn hồ sơ thật, năm PDF. Không sửa hàng đợi crawl gốc.

| Hồ sơ | Ca thử | Kết quả bước đọc |
| --- | --- | --- |
| 113/2026/VBHN-TT-NHNN | PDF native | Qua OCR/định danh, đủ điều kiện sang LLM |
| 153/2026/TT-BTC | Scan, số viết tay từng thành A53 | Đọc vùng hẹp sửa đúng 153; đủ điều kiện sang LLM |
| 55/2026/TT-NHNN | Scan, ngày từng thành 22/10 | Ngày đã khớp 12/10; vẫn chặn `ocr_needs_review` trang 4 |
| 77/2026/TT-BXD | Phụ lục được liệt kê trước văn bản chính | Bỏ phụ lục, đọc PDF chính; số 77 và ngày 09/10 khớp; vẫn chặn trang bảng 36 |

Chỉ thử file PDF tiếp theo khi file trước được xác định là phụ lục thiếu văn bản chính. Sai số/ngày, lỗi nội dung hoặc checksum không được dùng để bỏ qua sang file khác. Các lần chọn/bỏ file lưu tại `v2_work/<sha256>/pdf-selection.json`; bằng chứng OCR ở `ocr-review.json` cùng thư mục.

Hai ca qua OCR **chưa phải bản nháp đã hợp lệ để ghi**: còn cần danh mục, mapping sống, LLM, embedding và kiểm tra trùng trên ES.

## 1. Kiểm tra kết nối và đầu vào, không ghi ES/MinIO

Chạy bằng môi trường Python đã cài `requirements.txt`:

```bash
python -m legal_crawler.v2.preflight \
  --data data_es2_test/input --check-ocr \
  --out data_es2_test/preflight.json
```

Lệnh kiểm tra cluster UUID, trạng thái ES, mapping của đúng ba index cụ thể, LLM `/models`, OCR, MinIO/bucket riêng, checksum các PDF. Embedding sinh thử một vector, kiểm tra đúng BGE-M3, 1024 chiều, `max_length=256`. `--check-ocr` chạy thêm đầu vào qua bộ chặn OCR/định danh; không gọi LLM trích xuất.

Lệnh không tạo index, bucket hoặc bản ghi; không có tham số ghi. Exit `2` nghĩa còn blocker; không coi kết nối lỗi hoặc draft rỗng là test đạt. Nếu đọc được mapping, snapshot được lưu cạnh báo cáo để đối chiếu. Báo cáo không ghi thông tin xác thực vào lỗi.

`ready_for_export` kiểm tra điều kiện dựng nháp; MinIO riêng lẻ bị lỗi không ngăn dựng nháp khi ES/LLM/OCR/embedding đã thông. `ready_for_write_test` còn cần MinIO, draft hợp lệ và có văn bản mới sau kiểm tra trùng.

Lượt chuẩn bị 20/10/2026: OCR, LLM `/models` và sinh thử embedding hoạt động; ES `10.1.10.23:9200` và endpoint MinIO trong `.env` bị `ConnectTimeout` từ máy chạy. Vì vậy chưa đọc mapping/danh mục sống, chưa dựng full draft, chưa test ghi thật. Cần thông kết nối của máy chạy tới hai dịch vụ này trước bước 2; giữ nguyên đích ES 23.

Cập nhật 20/10/2026 lúc 09:27 UTC: người dùng cung cấp endpoint Tailscale `100.77.4.35:9200` cho nguồn và đích. Ping 3/3 gói, trung bình 10,79 ms; ES trả HTTP 200, phiên bản 8.13.4, health `yellow`; đọc được mapping của cả ba index v2. `.env` giữ `ES_WRITE_ENABLED=no`, MinIO vẫn là `10.1.10.10:9000`. [Preflight mới](../data_es2_test/preflight-tailscale.json) còn chặn bởi MinIO và embedding bị `ConnectTimeout`; embedding có phản hồi health khi kiểm riêng nhưng chưa qua preflight. OCR/LLM hoạt động, bộ mẫu vẫn hai ca đủ điều kiện và hai ca quarantine. Chưa ghi ES/MinIO hoặc dựng full draft.

## 2. Dựng bản nháp thật khi preflight đã thông

```bash
python -m legal_crawler.v2.export \
  --data data_es2_test/input \
  --out data_es2_test/draft --limit 4
```

Export thực hiện OCR/định danh → LLM kèm dẫn chứng → chunk → embedding → quan hệ → validation mapping v2 → so trùng. ID chỉ cấp sau khi hỏi ES về tài liệu, chunk và quan hệ đang tồn tại; không tự chọn ID khi ES chưa trả lời. `--no-embed` không dùng cho bộ test ghi.

Kiểm tra `summary.json`, `quarantine.json`, `invalid.json`, `warnings.json`, `compare.json` và các JSONL. Hai ca cần duyệt không được lọt vào `documents.jsonl`. Không đổi số hiệu/ngày hoặc thêm tiền tố giả để tránh trùng; nếu văn bản đã tồn tại thì publisher phải bỏ qua.

## 3. Xác minh draft và chạy thử publisher không ghi

```bash
python -m legal_crawler.v2.preflight \
  --data data_es2_test/input --draft data_es2_test/draft \
  --out data_es2_test/before-write.json

python -m legal_crawler.v2.publish \
  --data data_es2_test/input --draft data_es2_test/draft \
  --limit 1 --report data_es2_test/publish-dry-run.json
```

Preflight kiểm tra mapping sống, ID/nguồn gốc, vector, PDF/hash/đường dẫn MinIO, quan hệ/chunk đúng tài liệu và cùng lần chạy; sau đó gọi publisher với `write=False`. Nếu toàn bộ là trùng/ID đã dùng thì `ready_for_write_test=false`, không coi đó là test tạo mới thành công. Publisher từ chối draft thiếu vector hoặc không có chunk trước khi ghi.

## 4. Lệnh test ghi một văn bản đã kiểm tra

Chỉ chạy bước này khi báo cáo đã sẵn sàng và có yêu cầu thực hiện test ghi. Chọn ID thật của 153/2026/TT-BTC trong draft nếu chưa trùng, để kiểm tra cả đường sửa OCR; nếu đã trùng thì chọn một văn bản thật khác đã kiểm tra.

```bash
ES_WRITE_ENABLED=yes python -m legal_crawler.v2.publish \
  --data data_es2_test/input --draft data_es2_test/draft \
  --only <ID_TRONG_DRAFT> --write \
  --report data_es2_test/publish-write.json
```

Hai chốt `--write` và `ES_WRITE_ENABLED=yes` vẫn bắt buộc. Không bật lịch nightly hoặc đổi `.env` sang ghi mặc định để làm test. Không tạo index thử hoặc đổi mapping production.

Publisher kiểm tra trùng lần nữa, kiểm hash trước mọi thao tác MinIO, tải PDF lên bucket riêng (từ chối `legal-upload`), `_create` chunk/document/quan hệ và đọc lại kiểm số lượng/hash/vector. `publish-write.json` ghi rõ trạng thái và chính xác ID đã tạo; `dry_run`, `duplicate`, `exists` không được ghi nhận là đã tạo.

## Hoàn tác và tiêu chí đạt

Khi lỗi, publisher chỉ hoàn tác các `_id` đã được trả về tạo thành công trong chính lần chạy, không xóa theo khoảng ID hoặc query. PDF theo hash có thể được dùng chung nên không tự xóa PDF khỏi MinIO khi rollback ES.

Sau test thành công, đây là văn bản thật mới trong index production. Nếu cần dọn riêng, dùng các ID `created_*` của receipt `written`, đối chiếu `ingest_job_id`/hash đang lưu trước khi dọn; không dùng danh sách ID dự kiến từ dry-run. Lượt chuẩn bị này chưa tạo dữ liệu trên ES/MinIO nên không có gì cần xóa.

Đạt test ghi khi có receipt `written` của văn bản thật, số/ngày khớp nguồn và bằng chứng crop, schema/vector hợp lệ, đủ chunk/quan hệ, PDF đúng hash và chạy lại trả `exists` hoặc `duplicate`. Các ca quarantine vẫn bị chặn trước LLM/publish. Kiểm thử mock hoặc preflight không thay thế lần xác nhận ghi/đọc lại trên cụm thật.

- [Kết quả preflight tại workspace](../data_es2_test/preflight.json)
- [Danh sách ca thử](../data_es2_test/fixtures.json)
- [Quyết định hiện hành](adr/0002-ocr-and-es2-test.md)
