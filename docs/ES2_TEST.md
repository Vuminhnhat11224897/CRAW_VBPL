# Chuẩn bị test ghi v2 vào cụm 23

Luồng đã tích hợp kiểm tra OCR/số hiệu/ngày vào `build.prepare`, trước LLM, embedding, quan hệ và publish. Đích là ba index v2 trên cụm 23 qua Tailscale `100.77.4.35:9200` (IP LAN `10.1.10.23:9200`): `legal-docs`, `legal-chunk-section-v2`, `legal-changed-v2`. Export mặc định chỉ dùng mapping/đối chiếu v2, không cần các index v3.

## Kết quả mới nhất: 10 văn bản ngày 21/10/2026

Đã tải mới 10 hồ sơ ban hành ngày 20–21/10/2026, tổng 129 trang PDF, sau kiểm tra trùng ES. Lượt đầu chỉ một hồ sơ đủ điều kiện ghi; sau đối chiếu PDF và hiệu đính có bằng chứng, đã tạo thêm 9 document và cập nhật bản `10100001` / `27/2026/TT-BNV` bằng điều kiện phiên bản, có bản sao lưu trước sửa.

Đọc lại đủ 10 document, **1.018 chunk, 153 quan hệ và 1.028 vector**: mọi field khớp bản đã duyệt, vector đúng 1024 chiều và khớp trong dung sai số thực; PDF tải lại từ MinIO đều đúng SHA-256. Publish lần nữa trả `exists=10`, không tạo thêm dữ liệu. Mapping và `.env` không đổi. **173 kiểm thử đạt**.

Đã đối chiếu số hiệu, ngày ban hành, cơ quan và khối chữ ký của cả 10 PDF; duyệt 7 trang OCR bị cảnh báo. Nghị định `392/2026/NĐ-CP` được phục hồi đủ 51 điều và 2 phụ lục khi OCR làm sai trạng thái ngoặc kép; nghị định `391/2026/NĐ-CP` đủ 65 điều. Phụ lục Thông tư 27 được dựng lại từ ô bảng PDF, đủ 31 dòng; tên/chức vụ người ký không còn bị đưa vào nơi nhận. Đây là kiểm tra các trường và cấu trúc đã nêu, không chứng nhận từng từ trên toàn bộ 129 trang.

Ngoài `reviewed-corrections.json`, luồng hỗ trợ `reviewed-pages.json` và `reviewed-metadata.json` trong thư mục làm việc theo SHA-256 của PDF. Bản duyệt phải khớp hash PDF, văn bản đầu vào và ảnh bằng chứng; các biên điều/phụ lục được duyệt phải khớp chính xác tiêu đề trong văn bản. Duyệt trang chỉ giải quyết các cảnh báo đã được chỉ rõ, không tự bỏ qua lỗi mới. Tên cơ quan mới chỉ được chấp nhận khi có duyệt PDF và khớp cơ quan trên trang nguồn.

Chi tiết và receipt tại thư mục dữ liệu cục bộ `data_latest10/20261021T075336Z/review/`, báo cáo `final-report.json`; dữ liệu thử và ảnh không đưa vào Git. Kết quả này có bước duyệt riêng, chưa xác nhận vận hành tự động không giám sát: cơ chế làm mới danh sách, thứ tự mới nhất trước và thử lại publish vẫn cần hoàn thiện trước khi bật nightly.

## Lượt ghi một văn bản trước đó ngày 21/10/2026

**Bản thử trước đã hiệu đính và cập nhật ES:** bản `10100000` revision 2 có 86 chunk, 87 vector và đủ người ký Nguyễn Đức Chi. Rà ảnh PDF phát hiện lỗi OCR đổi nghĩa, mất điểm a Điều 3 và lẫn số trang; đã hiệu đính 16 mục theo bằng chứng, dựng lại metadata/chunk/embedding rồi đọc lại ES xác nhận. 166 kiểm thử đạt. Có snapshot trước sửa và nhật ký ghi có kiểm tra phiên bản. [Báo cáo hiệu đính](../data_es2_e2e/20261021T023611Z/accuracy-fix/REPORT.md). Các số liệu 81 chunk và signers=null bên dưới là lịch sử lần ghi đầu.

Pipeline chặn chuỗi điểm thiếu nhãn bằng `ocr_structure_needs_review`, bỏ số trang OCR khớp ở đầu trang, và tách cache metadata theo hash toàn văn. Hiệu đính có duyệt được đặt tại `<work>/<pdf_sha256>/reviewed-corrections.json`: phải khớp hash PDF, hash văn bản đầu vào/đầu ra, từng anchor và số lần xuất hiện; mỗi sửa có trang, lý do và người duyệt. Không tự động thay từ OCR trên toàn kho. Publisher thường vẫn chỉ tạo mới; sửa bản thử lần này dùng script riêng, giới hạn đúng các ID của lượt thử được người dùng yêu cầu sửa.

**Đã xác nhận full luồng cho một văn bản thật:** crawl mới `153/2026/TT-BTC` từ nguồn Chính phủ qua TMProxy → OCR 8 trang → LLM → 13 điều, 81 chunk → 82 vector BGE-M3 → QA một quan hệ → upload PDF MinIO → ghi/đọc lại ES2 cụm 23. Document ID **`10100000`**, 81 chunk và một quan hệ tới `37/2019/TT-BTC` / ES ID `420312`; receipt trả `written=1`.

Xác minh độc lập đọc lại toàn bộ document/chunk/quan hệ và vector, tải PDF từ MinIO đối chiếu SHA-256: đạt. Publish lần hai trả `exists=1`, không tạo thêm dữ liệu. Mapping và `.env` giữ nguyên, `ES_WRITE_ENABLED=no`; chỉ bật ghi cho tiến trình test. `signers` vẫn `null` với cảnh báo `signers_missing`. Lượt này dùng nguồn Chính phủ, chưa kiểm tra tải thật từ adapter `vbpl.vn`. Các lượt sau cần dựng nháp mới và kiểm tra trùng lại.

[Báo cáo, receipt ghi thật và bằng chứng đọc lại](../data_es2_e2e/20261021T023611Z/REPORT.md).

Cập nhật 21/10/2026: đã tìm và sửa lỗi bằng chứng OCR vùng chữ ký không được truyền sang metadata. Chạy lại LLM thật đọc đúng `KT. BỘ TRƯỞNG, THỨ TRƯỞNG, Nguyễn Đức Chi`; 81 chunk cục bộ nhận đủ người ký, toàn văn giữ nguyên, 162 kiểm thử đạt. Bản ghi ES của lượt thử trước chưa cập nhật. [Báo cáo sửa người ký](../data_es2_e2e/20261021T023611Z/signature-fix/REPORT.md).

## Kết quả chạy luồng ngày 20/10/2026

Đã chạy OCR/định danh → LLM thật → chia đoạn → BGE-M3 → QA quan hệ → validation mapping sống → kiểm trùng → publisher dry-run. Hai bản nháp hợp lệ: `10100000` / 113/2026/VBHN-TT-NHNN có 12 điều, 36 chunk; `10100001` / 153/2026/TT-BTC có 13 điều, 81 chunk. Tổng 119 vector hợp lệ 1024 chiều, một quan hệ từ Điều 13 Khoản 2 tới 37/2019/TT-BTC khớp ES ID `420312`. Số/ngày cả hai khớp chính xác metadata nguồn và bằng chứng OCR. Hai ca cần duyệt vẫn quarantine, không vào draft.

Test phát hiện và sửa hai lỗi tích hợp: parser không nhận tiêu đề OCR có `#`/`**`, khiến 153 chỉ có 4/13 điều; QA quan hệ thiếu định danh nguồn, nhầm “Thông tư này” với văn bản đích. Parser hiện diễn giải định dạng trên nhãn cấu trúc mà không đổi `full_content`; QA nhận số/ngày/tên văn bản nguồn và không dùng lại cache thiếu ngữ cảnh. 156 kiểm thử đạt.

Publisher trả `dry_run` cho cả hai, không có ID được tạo. Đọc lại ES xác nhận hai document chưa tồn tại, không có chunk/quan hệ mang hai ID này; mapping không đổi so với đầu lượt test. `ES_WRITE_ENABLED=no`. MinIO `10.1.10.10:9000` vẫn timeout nên chưa test upload PDF hoặc ghi/đọc lại ES thật. Embedding đã sinh vector cho cả hai draft và qua preflight cuối, nhưng trước đó có timeout gián đoạn. 153 còn cảnh báo `signers_missing`, giữ `null`; chưa có bằng chứng đủ chắc để tự điền người ký.

[Báo cáo và bằng chứng tại workspace](../data_es2_test/FLOW_TEST_REPORT.md). Lượt này xác nhận luồng và các bộ chặn trên bốn hồ sơ, không chứng nhận độ đúng OCR 95% trên mọi trang.

## Bộ dữ liệu đã chuẩn bị

Thư mục làm việc `data_es2_test/input` có SQLite riêng và bản sao đã kiểm SHA-256 của bốn hồ sơ thật, năm PDF. Không sửa hàng đợi crawl gốc.

| Hồ sơ | Ca thử | Kết quả bước đọc |
| --- | --- | --- |
| 113/2026/VBHN-TT-NHNN | PDF native | Qua OCR/định danh, đủ điều kiện sang LLM |
| 153/2026/TT-BTC | Scan, số viết tay từng thành A53 | Đọc vùng hẹp sửa đúng 153; đủ điều kiện sang LLM |
| 55/2026/TT-NHNN | Scan, ngày từng thành 22/10 | Ngày đã khớp 12/10; vẫn chặn `ocr_needs_review` trang 4 |
| 77/2026/TT-BXD | Phụ lục được liệt kê trước văn bản chính | Bỏ phụ lục, đọc PDF chính; số 77 và ngày 09/10 khớp; vẫn chặn trang bảng 36 |

Chỉ thử file PDF tiếp theo khi file trước được xác định là phụ lục thiếu văn bản chính. Sai số/ngày, lỗi nội dung hoặc checksum không được dùng để bỏ qua sang file khác. Các lần chọn/bỏ file lưu tại `v2_work/<sha256>/pdf-selection.json`; bằng chứng OCR ở `ocr-review.json` cùng thư mục.

Qua OCR chỉ đủ điều kiện sang LLM; bản nháp còn phải qua danh mục, mapping sống, embedding và kiểm tra trùng trên ES. Hai ca đã chạy đủ các bước này trong lượt test nêu trên; ghi thật vẫn cần MinIO và kiểm tra lại ngay trước publish.

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
