# ADR 0001 — Ghi dữ liệu theo schema v2 vào cụm v3

> Luồng hiện hành được chốt tại [ADR 0002](0002-ocr-and-es2-test.md). Các mục nhắc đích v3, mốc ID cũ hoặc ngày ký chưa xác minh bên dưới là lịch sử, không dùng để chạy test hiện tại.

- Trạng thái: **Đề xuất**; cập nhật 19/10/2026 theo chỉ đạo mới (mục 0)
- Ngày: 2026-10-15
- Phạm vi: luồng crawl chạy tự động 00:00 hằng đêm, bước chuẩn hóa và ghi vào Elasticsearch.

## 0. Chỉ đạo ngày 19/10/2026 (thay thế các điểm cũ trái ngược)

1. **Đích ghi là bộ index v2 production trên cụm 23:** `legal-docs` và `legal-chunk-section-v2` (không còn ghi `*-v3`; `legal-changed-v2` chưa ghi vì luồng chưa tạo quan hệ).
2. **Cụm 10 (`10.1.10.10`) bị cấm tuyệt đối:** mã từ chối mọi URL của cụm đó, đọc lẫn ghi (`legal_crawler/v2/safety.py`).
3. **PDF lên bucket MinIO riêng** (mặc định `legal-crawler`, không dùng `legal-upload` của `data_vbpl`); đường dẫn `<bucket>/pdf/<sha256>.pdf` ghi vào `pdf_minio_path`.
4. **Chốt chặn ghi:** chỉ ghi khi chạy `--write` **và** `ES_WRITE_ENABLED=yes`; chỉ ghi ID ≥ 1.000.000.001 với `origin=crawler`; tạo mới (`_create`), không bao giờ ghi đè; lỗi thì chỉ hoàn tác những gì lần chạy đó vừa tạo.
5. Các mục 2 (quyết định) và 8 bên dưới nhắc đến `*-v3` là bản trước chỉ đạo này.

## 1. Bối cảnh

Luồng thu thập văn bản (vanban.chinhphu.vn, congbao.chinhphu.vn, vbpl.vn) sẽ chạy ngầm trong container mỗi đêm lúc 00:00. Dữ liệu ghi vào phải **đúng định dạng v2 hiện hành** và không được làm sai dữ liệu đang có.

Có hai cụm Elasticsearch, hai tên dễ nhầm lẫn:

| Tên dùng trong tài liệu này | Là gì | Địa chỉ | Index |
| --- | --- | --- | --- |
| **v2** (chỉ đọc) | Bộ index production hiện tại, 518.261 văn bản | `SOURCE_ES_URL=http://10.1.10.23:9200` | `legal-docs`, `legal-chunk-section-v2`, `legal-changed-v2` |
| **v3** (ghi) | Bộ index mới, **cùng cụm** | `TARGET_ES_URL=http://10.1.10.23:9200` | `legal-docs-v3`, `legal-chunk-section-v3`, `legal-changed-v3` |
| **Schema v2** | Hình dạng bản ghi (field, kiểu, quy tắc chunk) do backend `data_vbpl` ghi | — | — |

**Từ 19/10/2026 mọi việc đọc và ghi của luồng này nằm trên cụm 23** (`10.1.10.23`). Cụm 10 (`10.1.10.10`) không còn dùng; bộ v2 trên cụm 23 có số lượng trùng cụm 10 (518.261 / 10.331.917 / 180.452). Vì cùng một cụm nên mọi truy vấn ghi phải chỉ rõ tên index `*-v3`, không dùng alias hay mẫu tên.

Quy ước: "v2" luôn là **schema/nguồn**, "v3" luôn là **cụm đích**. Không dùng "ghi vào v2".

Hiện trạng đã kiểm tra (15/10/2026, chỉ đọc):

- Mapping `legal-docs-v3`, `legal-chunk-section-v3` là **tập cha** của mapping v2: cùng field và kiểu, thêm các field riêng của v3 (`ngay_hieu_luc`, `cap_hieu_luc`, `la_vbqppl`, `hoi_to*` ở document; `dan_chieu`, `so_doan` ở chunk). Không có field v2 nào thiếu hay khác kiểu. `legal-changed-v3` khác v2 ở 3 field (`scope_resolution_method`, `target_issuing_authority` đổi từ `text` sang `keyword`) và thêm nhiều field.
- Đích v3 **đã có dữ liệu**: 203 văn bản (202 `origin=legacy` và 1 `origin=user_upload`, đều do `migration-v3`), 6.288 chunk, 733 quan hệ. Cụm còn có `*-v4` (532 văn bản). **Không đụng tới v4.**
- Backend `data_vbpl` vẫn đang ghi vào nguồn v2 (6 văn bản `origin=user_upload`, ID `900000001`–`900000017`), cấp ID từ sequence PostgreSQL riêng.

## 2. Quyết định

1. **Đọc từ nguồn v2, ghi vào đích v3.** Nguồn v2 chỉ dùng để lấy danh mục (loại văn bản, cơ quan, lĩnh vực) và kiểm tra trùng. Không ghi, không xóa, không sửa bất kỳ index nào ở nguồn v2.
2. **Bản ghi đúng schema v2, không điền field riêng của v3.** Các field chỉ có ở v3 để trống; việc điền do quy trình khác.
3. **Chỉ ghi `legal-docs-v3` và `legal-chunk-section-v3`.** `legal-changed-v3` (quan hệ giữa văn bản) **chưa ghi**: cần bước QA bằng LLM mà luồng này chưa có.
4. **Logic dựng bản ghi lấy nguyên từ `data_vbpl`** (commit `22d9796`), sao chép vào `legal_crawler/v2/`: parser cấu trúc, chia chunk, chuẩn hóa metadata, quy tắc `action`, bóc metadata bằng LLM có dẫn chứng, embedding BGE-M3.
   - **Bóc metadata bằng LLM Lào Cai** (`http://100.91.202.88:8012/v1`, model `qwen3.6-35b`, tắt thinking, 4000 token, timeout 5 s kết nối / 30 s đọc) theo đúng quy trình của v2: LLM trả JSON theo schema kèm dẫn chứng nguyên văn, mỗi trường được kiểm tra lại với văn bản PDF, sai thì yêu cầu sửa tối đa 3 lần.
   - **Số request LLM song song:** 3 ban ngày, 10 từ 22h đến 6h sáng (`LLM_CCU`, `LLM_CCU_NIGHT`).
   - **Đối chiếu chéo với trang nguồn:** số hiệu, ngày ban hành, loại văn bản và cơ quan do LLM đọc từ PDF phải trùng với metadata trang web; lệch thì vào hàng chờ `metadata_conflict`, không ghi.
   - **OCR cho PDF scan: Vintern** (`/ocr/raw` lấy text, `/ocr/boxes` lấy tọa độ), `OCR_API_URL` mặc định `http://127.0.0.1:8000`. Đã tích hợp (`legal_crawler/v2/ocr.py`); PDF không có lớp chữ được OCR, `text_source=ocr`. Nếu máy chủ OCR không trả lời thì văn bản vào hàng chờ `ocr_unavailable` và **tự thử lại đêm sau**, không đoán. Máy chủ OCR chỉ nghe `127.0.0.1` của máy `100.91.202.88` và không có xác thực, nên nơi chạy luồng này phải cùng máy đó hoặc có đường hầm SSH tới cổng 8000.
   - **Quy tắc ngày** (`legal_crawler/v2/dates.py`): ngày hiệu lực lấy từ trang web, nếu thiếu thì từ PDF; ngày ban hành đối chiếu ở góc trên-phải trang 1 (`Hà Nội, ngày … tháng … năm …`); ngày ký lấy từ dấu ký số của cơ quan hoặc dòng `địa danh, ngày …` ở góc dưới-phải trang cuối, và phải cách ngày ban hành không quá 60 ngày. Ngày hiệu lực từ PDF chỉ nhận câu của chính văn bản (`… này có hiệu lực …`), không nhận câu trích dẫn; **văn bản hợp nhất không có ngày hiệu lực riêng**. Dấu ký số của Công báo là chữ ký của Công báo, không dùng làm ngày ký. Các ngày này dùng để lọc phạm vi (từ 2024) và để đối chiếu, **không ghi vào `_source`** (`ngay_hieu_luc` chỉ có ở v3, xem mục 8).
5. **Quy tắc trùng:** cùng **số hiệu chuẩn hóa + cùng ngày ban hành** thì coi là cùng nội dung (chốt ngày 15/10/2026). Kiểm tra trên **cả nguồn v2 và đích v3**. Văn bản trùng thì **lưu metadata, không tải file, không ghi vào đích v3**. Khác với `data_vbpl` (thêm điều kiện cơ quan ban hành), xem rủi ro ở mục 7.
6. **ID văn bản:** dải riêng của crawler, bắt đầu `1.000.000.001`, tách khỏi dải `900000001+` của `data_vbpl`; giữ bảng `identity → doc_id` bền vững để chạy lại không sinh ID mới. ID chunk theo mẫu `<doc_id>[_pl<i>][_s<n>]_d…`, `is_full`, `segment` đúng hợp đồng v2.
7. **Dấu vết nguồn gốc:** `origin=crawler`, `uploaded_by=crawler`, `ingest_job_id=crawler:<thời điểm UTC của lần chạy>`, `ingest_revision=1`. Giá trị `origin` này là giá trị mới (hiện có `legacy`, `user_upload`); mapping là `keyword` nên được chấp nhận.
8. **Không chắc thì không ghi.** Mỗi văn bản không đạt kiểm tra bị đưa vào hàng chờ xử lý (quarantine) kèm lý do, không ghi vào đích v3.

## 2a. Chế độ nháp (hiện hành)

Luồng chạy trọn vẹn nhưng **dừng ở bản nháp**: `python -m legal_crawler.nightly [--once]` (chạy lúc `NIGHTLY_AT`, mặc định 00:00) gọi crawl rồi `legal_crawler.v2.export --new-only`. Kết quả mỗi đêm nằm ở `data/v2_draft/<thời điểm UTC>/`:

| Tệp | Nội dung |
| --- | --- |
| `documents.jsonl`, `chunks.jsonl` | Đúng hình dạng sẽ ghi vào `legal-docs-v3` và `legal-chunk-section-v3`, đã có vector |
| `compare.json` | Với từng bản nháp: đã có trong v2/v3 chưa (số hiệu + ngày), nếu có thì khác ở field/chunk nào; ngày theo trang web và theo quy tắc góc trang |
| `quarantine.json` | Hàng chờ kèm lý do (`needs_ocr` đã được thay bằng OCR, `ocr_unavailable`, `metadata_conflict`, `llm_extraction_failed`, `outside_date_range`, …) |
| `invalid.json`, `warnings.json`, `summary.json` | Lỗi schema (phải rỗng), cảnh báo, tóm tắt |

`data/v2_draft/state.json` giữ ID tạm đã cấp và các văn bản bị loại vĩnh viễn; lý do tạm thời (OCR, LLM) được thử lại. `Dockerfile` và `docker-compose.yml` đóng gói luồng này (mạng host để thấy cổng OCR); chưa build, chưa chạy.

## 3. Nguồn từng field của bản ghi

| Field (document) | Lấy từ | Ghi chú |
| --- | --- | --- |
| `document_number`, `issuance_date`, `title`, `legal_type`, `issuing_authority` | **LLM đọc PDF** (kèm dẫn chứng nguyên văn), chuẩn hóa bằng `normalize_metadata` của v2, rồi đối chiếu với metadata trang nguồn | Loại văn bản và cơ quan **phải có trong danh mục của nguồn v2**; ngày đổi sang `YYYY-MM-DD`. Lệch với trang nguồn thì vào hàng chờ |
| `signers` | **LLM đọc khối chữ ký trong PDF**, mỗi thành phần phải có trong văn bản và trong dẫn chứng | v2 lưu cả khối, ví dụ `TM. CHÍNH PHỦ, KT. THỦ TƯỚNG, PHÓ THỦ TƯỚNG, Phạm Thị Thanh Trà` |
| `action` | Tính từ `title` + `legal_type` theo luật của v2 | |
| `full_content`, `can_cu`, `can_cu_refs`, `intro`, `noi_nhan`, `phu_luc`, `has_tail`, `n_dieu`, `grade` | Parser của v2 chạy trên lớp chữ của PDF | |
| `text_source` | `text_layer` (PDF có lớp chữ) | `ocr` khi tích hợp Vintern |
| `content_sha256`, `pdf_minio_path` | SHA-256 của file; `<bucket>/pdf/<sha256>.pdf` | **Object phải được tải lên MinIO trước khi ghi ES** (chưa làm, xem mục 6) |
| `url` | `source_url` thật của trang nguồn | Hợp đồng v2: nguồn thật nếu có |
| `embedding` | BGE-M3 (1024 chiều) trên `title` | Chunk: `tieu_de + " " + noi_dung`, gộp khoảng trắng |
| `legal_sectors` | **`[]` mặc định** (`LEGAL_SECTORS_MODE=empty`); bật `llm` thì lấy kết quả LLM | LLM Lào Cai chọn thừa nhãn so với dữ liệu đã duyệt của v2 (mục 4, điểm 4); để trống là hợp lệ theo hợp đồng v2, thiếu dữ liệu chứ không sai |
| `tom_tat` | LLM, bắt buộc không rỗng (v2 cũng bắt buộc) | |
| `pdf_vbpl_id` | `null` | Chỉ giữ khi có nguồn xác thực |

## 4. Bằng chứng tương thích (đã chạy 15/10/2026)

1. **Tái tạo đúng 6/6 văn bản thật của v2.** Chạy parser và bộ chia chunk đã sao chép trên `full_content` của 6 văn bản `origin=user_upload` đang có ở nguồn v2, rồi so từng field của document và từng chunk (ID, loại, nội dung, tham chiếu). Kết quả: **khớp 100%** (51, 16, 29, 27, 57 và 4 chunk). Công cụ: `tools/v2_parity.py golden`.
2. **Mẫu 60 văn bản lịch sử không khớp** (`grade` khác ở 60/60, `action` thiếu ở phần lớn chunk cũ, nhiều khác biệt `intro`/`noi_nhan`). Đây là dữ liệu do pipeline batch cũ tạo ra; hợp đồng của `data_vbpl` ghi rõ "không sao chép lỗi thiếu field" của dữ liệu cũ. **Chuẩn tương thích là ghi hiện hành (6/6), không phải toàn bộ dữ liệu lịch sử.** Chạy: `tools/v2_parity.py sample`.
3. **Kiểm tra schema theo mapping thật** (`legal_crawler/v2/validate.py`): mọi bản ghi xuất ra được kiểm kiểu và tên field với mapping **cả nguồn v2 lẫn đích v3** (field lạ sẽ bị mapping động, nên bị chặn; ngày `yyyy-MM-dd`; vector 1024 chiều hữu hạn, khác 0; ID chunk không trùng, `doc_id` khớp).
4. **Xuất thử 30 văn bản từ bản thử** (`python -m legal_crawler.v2.export --limit 30`, không ghi ES): xem mục 4a.

4. **Bóc metadata bằng LLM Lào Cai so với dữ liệu v2 đã duyệt** (`tools/v2_meta_parity.py`, chạy lại `extract_metadata` trên `full_content` của ES, so từng trường):

| Nhóm | Trường khớp tuyệt đối |
| --- | --- |
| 6 văn bản do luồng v2 hiện hành công bố (người đã duyệt) | `document_number`, `title`, `legal_type`, `issuing_authority`, `issuance_date`, `signers`, `action`: **6/6 mỗi trường**; `tom_tat` có nội dung 6/6 |
| **`legal_sectors`** | **0/6**: model chọn thừa 1–7 nhãn so với bản đã duyệt (vd thêm "Bộ máy hành chính", "Tài nguyên - Môi trường") |
| 14 văn bản 2025–2026 ngẫu nhiên (pipeline cũ) | số hiệu, loại, cơ quan, ngày: 14/14. `title` khác 14/14 và `signers` khác vì **quy ước cũ** (tiêu đề kiểu "Quyết định 147/QĐ-UBND năm 2026 phê duyệt…", người ký dạng `Tên:id`); `legal_sectors` khớp 1/14 vì nhãn cũ không nhất quán (vd "Bất động sản" cho quy hoạch sử dụng đất) |

   Kết luận: các trường nhận diện, chữ ký và `action` đạt chuẩn v2 hiện hành. **`legal_sectors` chưa đủ tin cậy để ghi tự động**, nên mặc định để trống.

### 4a. Kết quả xuất thử 30 văn bản (có LLM)

Chạy 16/10/2026 trên 30 văn bản đầu của bản thử (`data_trial/`). Không ghi ES.

| Chỉ tiêu | Kết quả |
| --- | --- |
| Văn bản đưa vào | 30 |
| **Xuất được, qua kiểm tra schema v2 và v3** | **7** (3.399 chunk, mỗi bản ghi có vector 1024 chiều) |
| Sai schema | 0 |
| Hàng chờ `needs_ocr` (PDF scan, không có lớp chữ) | 20 (67%) |
| Hàng chờ `metadata_conflict` | 2: `128/2026/VBHN-NĐ-BXD` (LLM đọc cơ quan ban hành là "Chính phủ", trang nguồn ghi Bộ Xây dựng) và `25/2026/TT-BNV` (PDF in số hiệu trống, ngày dự thảo 25/09 khác ngày 01/10 trên trang nguồn) |
| Hàng chờ `llm_extraction_failed` | 1: `127/2026/QĐ-UBND` (số hiệu trống trong PDF, dẫn chứng ngày bị từ chối) |
| Cảnh báo ở bản xuất được | 1: `172/2026/VBHN-LQ-VPQH` `grade_partial` (490 Điều) |

Chữ ký ở 7/7 bản là cả khối (kể cả `43/2026/NQ-CP`, nơi cách đọc theo bố cục trước đó mất chức danh). Bước đối chiếu chéo với trang nguồn đã chặn một lỗi thật của LLM (`128/2026`). Mỗi văn bản tốn khoảng 11–35 giây LLM (3 song song), nên 1.000 văn bản/đêm ở mức 3 song song cần khoảng 2–3 giờ; ở mức 10 song song vào ban đêm thì ngắn hơn.

## 5. Hệ quả

**Tốt:** bản ghi cùng định dạng với dữ liệu v2 hiện hành (bằng chứng ở mục 4.1); chạy lại không tạo trùng; mọi lỗi để lại lý do.

**Hạn chế lớn nhất: chưa có OCR thì nhiều văn bản trung ương không ghi được.** Phần lớn PDF của vanban.chinhphu.vn là bản scan hoặc bản ký số không có lớp chữ đọc được; hiện chúng vào hàng chờ `needs_ocr` (tỉ lệ ở mục 4a). Đã chọn Vintern làm OCR; chưa tích hợp.

**Cần chấp nhận:** khi bật OCR sau này, văn bản đã vào hàng chờ chỉ cần chạy lại, không phải crawl lại.

## 6. Việc còn lại trước khi bật lịch 00:00

1. **Tải PDF lên MinIO** (`<bucket>/pdf/<sha256>.pdf`) và kiểm tra object trước khi ghi ES. Chưa có credentials trong dự án.
2. **Kiểm tra trùng trên đích v3** (hiện mới có bước kiểm tra nguồn v2 trong `legal_crawler/es.py`).
3. **Bảng cấp ID bền vững** (`identity → doc_id`) và kiểm tra ID chưa tồn tại ở **cả hai cụm** trước khi `_create`.
4. **Bước ghi**: bulk với `op_type=create`, đọc lại và so (`verify`), dừng ngay nếu có lỗi bulk; không bao giờ xóa. Bản ghi document và chunk của cùng một ID chỉ được coi là xong khi cả hai đều đọc lại khớp.
5. **Khóa chạy** để hai lần chạy không đè nhau; nếu lần chạy 00:00 chưa xong thì lần sau bỏ qua.
6. **Báo cáo mỗi đêm**: số mới ghi, trùng, hàng chờ theo lý do, lỗi; cảnh báo khi hàng chờ tăng đột biến.
7. **Kiểm tra OCR Vintern thật.** Mã tích hợp đã có và có test với máy chủ giả, nhưng ngày 16/10/2026 máy chủ ở `127.0.0.1:8000` không trả lời (từ chối kết nối), nên **chưa chạy thử trên PDF scan thật**. Cần khôi phục đường hầm hoặc chạy luồng trên máy `100.91.202.88`, rồi chạy lại bản nháp để đo chất lượng.
8. **Chế độ nháp phải được bỏ có chủ đích.** Hiện không có đoạn mã nào ghi vào ES. Bước ghi thật (mục 6.1–6.6) chỉ viết sau khi bạn xem báo cáo so sánh và xác nhận mục 8.

## 7. Rủi ro đã biết

- **Trùng số hiệu và ngày nhưng khác cơ quan** (đặc biệt `QĐ-UBND` của hai tỉnh) bị coi là một văn bản, văn bản thứ hai sẽ không được ghi. `data_vbpl` tránh việc này bằng cách so thêm cơ quan. Khuyến nghị thêm điều kiện cơ quan ban hành cho số hiệu không có phần cơ quan địa phương. Với văn bản trung ương, số hiệu có ghi năm thì kết quả không đổi.
- **Số hiệu không ghi năm** (vd `1964/QĐ-TTg`) trùng chuỗi giữa các năm: phải so ngày ban hành, không so số hiệu một mình.
- **Tên người ký/khối chữ ký:** khi bố cục chữ làm mất chức danh (đã gặp ở `43/2026/NQ-CP`), `signers` chỉ có họ tên và bị đánh cảnh báo `signers_roles_not_adjacent`; v2 sẽ lưu cả khối.
- **Dữ liệu v3 đang được người khác dùng** (203 văn bản của `migration-v3`, thêm `v4`): ghi thêm vào index đang dùng cần được chủ cụm đồng ý.
- **Danh mục**: văn bản có `legal_type` hoặc cơ quan không nằm trong danh mục nguồn v2 bị đưa vào hàng chờ, không tự thêm giá trị mới vào danh mục.

## 8. Cần xác nhận

1. **Dải ID** `1.000.000.001+` cho crawler có được không, hay dùng chung sequence với `data_vbpl`?
2. **Giá trị `origin`/`uploaded_by`** = `crawler`: đồng ý?
3. **Thêm điều kiện cơ quan ban hành** vào quy tắc trùng cho văn bản địa phương?
4. **`legal_sectors`:** để trống (mặc định hiện tại), hay bật nhãn của LLM dù chọn thừa? Muốn có nhãn đáng tin cần thêm bước người duyệt hoặc đổi model/prompt.
5. **Ngày hiệu lực:** ghi vào `ngay_hieu_luc` của v3 (kèm `ngay_hieu_luc_cach`, `ngay_hieu_luc_trich`) hay bỏ qua? Nếu ghi thì quyết định 2 cần sửa. Và "góc phần tư bên trái" là trên-trái hay dưới-trái?
6. **Được ghi vào các index `*-v3` đang có dữ liệu** của `migration-v3` không, hay cần index/alias riêng?
