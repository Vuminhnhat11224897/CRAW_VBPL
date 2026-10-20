# Crawl tài liệu pháp luật qua TMProxy

Thu thập file gốc từ `vanban.chinhphu.vn`, `congbao.chinhphu.vn`, `vbpl.vn`; chuẩn hóa metadata chung. Mốc mặc định: **ngày ban hành từ 01/08/2026**, tính cả ngày đầu. `DATE_TO` trống nghĩa là không đặt ngày kết thúc. Từ giai đoạn 2, luồng còn bóc nội dung, dựng bản ghi theo schema ES v2 và (khi bật cờ) ghi lên Elasticsearch cụm 23; xem mục *Luồng v2 và chạy hằng đêm* ở cuối và [ADR 0001](docs/adr/0001-v3-cluster-v2-schema.md).

## Chạy

Python **3.11+**, Linux. Chạy lệnh tại thư mục dự án:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# Chỉ tạo .env nếu chưa có; không ghi đè key đã điền.
test -f .env || cp .env.example .env
chmod 600 .env
```

Điền `TMPROXY_API_KEY` trong `.env`. Toàn bộ tham số vận hành nằm tại đây; biến môi trường của tiến trình có ưu tiên cao hơn `.env`. Giá trị là chuỗi literal; hỗ trợ dấu nháy bao quanh, không nội suy `$VAR`, không thực thi shell, không hỗ trợ comment cuối dòng.

```bash
.venv/bin/python -m legal_crawler check-config
.venv/bin/python -m legal_crawler crawl
.venv/bin/python -m legal_crawler status
.venv/bin/python -m legal_crawler export
.venv/bin/python -m legal_crawler migrate-files
```

`check-config` chỉ kiểm tra cấu hình, không kiểm tra mạng, không hiển thị key. `crawl` kiểm tra kết nối TMProxy trước khi tạo tác vụ. Không tự chuyển sang IP trực tiếp. API quản lý TMProxy được gọi qua HTTPS trực tiếp; mọi request lấy trang, chuyển hướng và file nguồn đi qua proxy.

Trong môi trường phát triển hiện tại đã chuẩn bị `.venv` từ thư viện có sẵn để chạy kiểm thử; máy triển khai nên cài bằng `requirements.txt` theo hướng dẫn trên.

## Tham số `.env`

| Biến | Ý nghĩa |
| --- | --- |
| `TMPROXY_API_KEY` | Key TMProxy; đã có ô trống trong file mẫu |
| `DATE_FROM`, `DATE_TO` | Ngày ban hành `YYYY-MM-DD`, bao gồm hai đầu; trống là không giới hạn đầu đó |
| `SOURCES` | Danh sách tên thư mục nguồn, phân cách bằng dấu phẩy |
| `CONFIG_DIR`, `DATA_DIR` | Thư mục config và dữ liệu, tính từ thư mục chạy lệnh |
| `MAX_PAGES` | Số tác vụ HTML tối đa **mỗi nguồn mỗi lần chạy**; mặc định 100, đặt **0** để xử lý hết hàng đợi |
| `REQUEST_DELAY` | Khoảng nghỉ tối thiểu giữa các HTTP request, giây |
| `REQUEST_TIMEOUT` | Timeout kết nối/đọc, giây |
| `REQUEST_RETRIES` | Tổng số lần thử HTTP, kể cả lần đầu |
| `MAX_FILE_MB`, `MAX_HTML_MB` | Giới hạn dung lượng từng file/trang |
| `RETRY_FAILED` | `true` để thử lại tác vụ lỗi ở lần chạy sau |
| `REFRESH` | `true` để đọc lại các trang đã xong, tìm cập nhật; đặt lại `false` khi muốn tiếp tục hàng đợi |
| `TMPROXY_API_URL` | API gốc TMProxy, mặc định `https://tmproxy.com/api/proxy` |
| `TMPROXY_LOCATION`, `TMPROXY_ISP` | Tham số vị trí/nhà mạng khi xin IP mới |
| `TMPROXY_MAX_WAIT` | Giới hạn chờ cooldown đổi proxy, giây |
| `USER_AGENT` | User-Agent gửi đến nguồn |
| `VBPL_PAGE_SIZE` | Số văn bản/trang API VBPL, mặc định 20 |
| `CONGBAO_DOWNLOAD_ALL` | Mặc định `false`: hoàn tất văn bản khi tải được một file; `true`: tải mọi file phát hiện được |

Lần đầu dùng `MAX_PAGES=100` để xem chất lượng dữ liệu. Muốn chạy hết phần phát hiện được, đặt `MAX_PAGES=0`. Số này tính tác vụ danh sách/chi tiết; các tab thuộc cùng văn bản có thể phát sinh thêm HTTP request.

Crawler chạy luân phiên theo thứ tự `SOURCES`: mỗi nguồn một tác vụ rồi chuyển sang nguồn kế tiếp. Nguồn hết việc hoặc đạt `MAX_PAGES` được bỏ qua; nguồn còn việc tiếp tục chạy. Một tác vụ chi tiết xử lý các file của văn bản đó trước khi nhường lượt; timeout/thử lại HTTP vẫn có thể làm lượt đó chậm. Khi sửa code, dừng tiến trình cũ bằng Ctrl+C rồi chạy lại để áp dụng; tiến độ đã lưu sẽ được dùng tiếp.

Nhấn Ctrl+C để dừng; chạy lại cùng lệnh để tiếp tục. Tác vụ đang làm chưa được đánh dấu xong; file tải dở được tải lại từ đầu. Những file đã tải thành công sẽ được kiểm tra SHA-256 và tái sử dụng. Hai tiến trình không được chạy cùng `DATA_DIR`.

Thử lại chỉ yêu cầu đổi IP khi gặp lỗi kết nối/timeout hoặc HTTP 403, 407, 429. Nếu proxy hiện tại còn hạn nhưng đang trong cooldown đổi IP, chương trình dùng tiếp proxy đó. Lỗi tải được ghi theo từng lần thử và giữ nguyên nguyên nhân đầu tiên nếu lần đổi proxy sau đó cũng thất bại.

Đổi khoảng ngày sẽ tạo phạm vi hàng đợi mới, để xem lại cả văn bản từng bị loại theo khoảng cũ. Kho tài liệu đã tải vẫn được giữ. JSONL là danh mục tích lũy, không chỉ riêng khoảng ngày của lần chạy gần nhất.

## Cấu hình từng nguồn

```text
config/
  vanban_chinhphu/source.json
  congbao/source.json
  vbpl/source.json
```

Mỗi file có URL bắt đầu, danh sách domain trang/file được phép, regex nhận diện trang chi tiết/danh sách/file/tab và CSS selector tiêu đề. Muốn thêm nguồn có HTML tương tự, thêm thư mục và tên vào `SOURCES`.

Crawler đọc liên kết HTML, file nhúng trong iframe/object, URL file trong `onclick`/`data-href`, và phân trang ASP.NET `__doPostBack`. VBPL hiện dùng adapter `vbpl_actions`: POST tới server action công khai của frontend, lọc `issueDateFrom`/`issueDateTo`, phân trang bằng `pageNumber`, đọc chi tiết và danh sách file rồi giải mã base64 về tài liệu gốc. Định danh văn bản theo ID trả về từ nguồn. Không cần trình duyệt hoặc dependency mới.

Với VBPL, `attachment_urls` là địa chỉ tham chiếu có fragment định danh văn bản/file; tải thật dùng POST. Metadata của mỗi file có thêm `request` ghi URL, action ID và arguments. Dùng file local khi ingest, không GET trực tiếp các địa chỉ tham chiếu này. Mã action nằm trong `config/vbpl/source.json`, lấy từ JavaScript chính nguồn; nếu VBPL triển khai phiên bản frontend mới, mã này có thể cần cập nhật.

Request VBPL gửi kèm `Next-Router-State-Tree` theo frontend; bộ đọc hỗ trợ phản hồi dạng array của Next.js 14. Ngày trong `.env` vẫn là `YYYY-MM-DD`; khi gọi VBPL, chuyển sang định dạng `API_TIME` của frontend: `DATE_FROM` lúc `00:00:00`, `DATE_TO` lúc `23:59:59`, ví dụ `2026-08-01T00:00:00`. Khi source trả HTTP lỗi, tối đa 64 KiB nội dung phản hồi được giữ lại; lỗi action lưu ở `raw/vbpl/*.error` để chẩn đoán, mã digest được ghi trong lỗi nếu nguồn có cung cấp, không in body hoặc key vào log.

Với Công báo và `CONGBAO_DOWNLOAD_ALL=false`, PDF gốc trên `congbaocdn.chinhphu.vn` được thử trước các link stream `g7`; khi tải được một file hợp lệ thì dừng tải thêm cho văn bản đó. Nếu các link trước lỗi nhưng link sau thành công, văn bản vẫn hoàn tất và lịch sử lỗi được giữ trong `errors`. Chọn `true` nếu cần cả các bản DOCX/tệp phụ. Xác minh chứng chỉ HTTPS vẫn bật.

Lọc ngày diễn ra sau khi đọc metadata, **trước khi tải file**. Không suy ra ngày ban hành từ tên file hoặc ngày đăng trang. Thiếu ngày khi đang lọc sẽ ghi `missing_date` để kiểm tra lại. Crawler chưa dừng sớm dựa vào thứ tự ngày của danh sách, nên có thể phải đọc metadata cũ để phát hiện hết văn bản trong khoảng yêu cầu.

## Dữ liệu đầu ra

```text
data/
  state.sqlite3          # Hàng đợi, trạng thái, danh mục file và văn bản
  raw/<nguồn>/*.html     # HTML để kiểm tra khi nguồn đổi giao diện
  files/vanban_chinhphu/<sha256>.pdf
  files/congbao/<sha256>.pdf
  files/vbpl/<sha256>.pdf   # Mỗi nguồn có cả pdf, doc, docx, rtf, zip
  metadata/<nguồn>/*.json
  documents.jsonl        # Một dòng/văn bản, ID ổn định cho bước ingest ES sau
```

Metadata chung gồm `schema_version`, `id`, `source`, `source_url`, `title`, `number`, `document_type`, `issuer`, `signer`, `issued_date`, `effective_date`, `summary`, `fetched_at`, `raw_pages`, `attachment_urls`, `files`, `errors`, `status`. Trường không đọc được có giá trị `null`.

Mỗi file lưu nguồn, URL gốc/cuối, đường dẫn tương đối với `DATA_DIR`, định dạng, MIME do server trả, kích thước, SHA-256 và thời gian tải. File trùng nội dung trong cùng nguồn dùng chung đường dẫn; hai nguồn có thư mục và cache riêng. Ghi file qua `.part` rồi đổi tên; không coi một trang HTML trả HTTP 200 là PDF hợp lệ. PDF kiểm tra header và EOF; DOC kiểm tra header OLE; DOCX kiểm tra cấu trúc ZIP tối thiểu. Đây là kiểm tra định dạng cơ bản, chưa xác thực chữ ký số hay nội dung pháp lý.

`migrate-files` chuyển kho cũ từ thư mục hash sang thư mục theo nguồn, không cần mạng/TMProxy. Lệnh tạo bản sao SQLite trước chuyển, kiểm tra SHA-256 của bản mới, cập nhật database, các JSON metadata và JSONL rồi mới xóa file cũ. Tiến độ và trạng thái tác vụ được giữ. Nếu bị ngắt, chạy lại để tiếp tục. `crawl`/`export` cũng tự áp dụng migration lần đầu. Khi crawler cũ còn chạy, `migrate-files` chờ khóa `crawler.lock` và tự chuyển sau khi tiến trình đó dừng; nhấn Ctrl+C ở terminal crawler để chuyển ngay. Các lệnh khác vẫn báo lỗi nếu khóa đang bị giữ.

Cache cũ không có hồ sơ để xác định nguồn được giữ trong `files/unassigned/`; khi nguồn yêu cầu lại URL đó, bản đã tải được tái sử dụng trong thư mục nguồn. File chưa được cache ghi nhận, chẳng hạn `.part` còn dở, không bị xóa trong migration.

| Trạng thái văn bản | Ý nghĩa |
| --- | --- |
| `downloaded` | Đã tải các file yêu cầu; riêng Công báo mặc định chỉ cần một file hợp lệ |
| `outside_date_range` | Không tải file vì ngày ban hành nằm ngoài khoảng |
| `missing_date` | Chưa đọc được ngày, cần kiểm tra HTML/config |
| `no_attachments` | Chưa tìm được file gốc, cần kiểm tra |
| `partial` / `failed` | Có lỗi tab hoặc tải file; lưu lỗi và thử lại lần sau |

`status` hiển thị số tác vụ `pending`, `done`, `failed`. Có `pending` nghĩa là còn việc; `done` không chứng minh đã bao phủ toàn bộ kho nguồn. Exit code: `0` không có tác vụ lỗi trong phạm vi vừa chạy (vẫn có thể còn pending), `1` lỗi cấu hình/khởi động, `2` có tác vụ lỗi, `130` dừng bằng Ctrl+C. Không chạy vòng lặp vô hạn để thử một lỗi; dùng lần chạy sau hoặc lịch cron do bạn quản lý.

## Kiểm thử và giới hạn xác nhận

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Các test dùng HTML/file mô phỏng và HTTP mock; không tiêu tốn quota TMProxy. Kiểm tra lọc ngày, config, phân trang, tab VBPL, chống trùng, tiếp tục sau lỗi, chặn HTML giả PDF, giới hạn domain/chuyển hướng và không lộ key trong lỗi.

**Chưa xác nhận end-to-end trên cả ba nguồn thật.** Chính phủ và Công báo đã ghi nhận tải file thành công qua TMProxy khi chạy từ terminal máy. Máy chủ stream `g7` của Công báo vẫn lỗi chứng chỉ; chế độ mặc định đã tải được PDF gốc từ domain khác. VBPL đã trả HTTP 500 với digest `748076738`; lỗi định dạng ngày thiếu giờ trong request đã được sửa theo hàm `API_TIME` của frontend, chưa xác nhận tải file thật sau sửa. Phiên trợ lý bị chặn tạo socket mạng (TCP/UDP báo `Operation not permitted`), nên kiểm thử mạng phải chạy từ terminal máy.

HTML tải thực tế ngày 13/10/2026 cho thấy Công báo dùng endpoint `/api/download/stream` cho PDF/DOCX; parser đã hỗ trợ và lấy metadata từ bảng thuộc tính để tránh lẫn menu. Regression test dùng phần HTML thực tế đã lưu.

**VBPL hiện trả giao diện JavaScript mới, kể cả khi gọi URL ASP.NET cũ.** Adapter đã được cập nhật theo các server action đọc từ frontend ngày 13/10/2026 và qua kiểm thử mô phỏng; chưa xác nhận tải file thật. Để thu thập mã frontend khi nguồn thay đổi, chạy trên máy có mạng:

```bash
.venv/bin/python -m legal_crawler.inspect_vbpl
```

Lệnh dùng key/proxy và giới hạn HTTP trong `.env`, lưu trang chủ cùng các JavaScript `/_next/static/` của VBPL vào `DATA_DIR/diagnostics/vbpl/`; không ghi đè hàng đợi và không cần dừng crawler. Nó phục vụ chẩn đoán, chưa phải luồng tải tài liệu VBPL.

Phân trang dựng hoàn toàn bằng JavaScript/API riêng, CAPTCHA và ViewState hết hạn chưa có adapter được xác nhận bằng dữ liệu thật. Nếu trang không có liên kết nhận diện được, crawler báo lỗi để kiểm tra, không báo đã thu thập đầy đủ.

Tham chiếu cấu trúc nguồn và API: [TMProxy API](https://tmproxy.com/api/doc/), [danh sách Chính phủ](https://vanban.chinhphu.vn/he-thong-van-ban?classid=1), [danh sách Công báo](https://congbao.chinhphu.vn/van-ban-dang-cong-bao.htm), [VBPL](https://vbpl.vn/). Giữ `source_url` khi sử dụng/xuất bản dữ liệu để truy ngược nguồn.

## Luồng v2 và chạy hằng đêm

Sau khi crawl, `legal_crawler.v2` đọc PDF (lớp chữ, hoặc OCR Vintern khi là bản scan), bóc metadata bằng LLM Lào Cai có dẫn chứng, chia chunk, tạo vector (BGE-M3), dò và thẩm định quan hệ giữa văn bản, rồi dựng bản nháp đúng schema v2.

PDF được chọn cách đọc **theo từng trang**: giữ lớp chữ tốt, trang cần OCR dùng `/ocr/raw`; tọa độ chỉ đọc ở trang đầu/cuối nếu các trang đó cần OCR. Kết quả OCR được cache theo nội dung PDF và cấu hình. Số hiệu/ngày ban hành phải khớp chính xác nguồn; khi chưa đọc được sẽ thử góc trên trái/phải và vùng hẹp quanh dòng, vẫn không xác minh được thì chặn `ocr_identity_unverified` trước LLM. Chỉ hai dòng định danh có bằng chứng khớp mới được sửa, giữ lại OCR thô. Nội dung đặt mục tiêu 95%, có kiểm tra thiếu/thêm token và cảnh báo bảng/công thức; các trang chưa đủ bằng chứng đi vào `ocr_needs_review`. Xem [cách chạy OCR](docs/OCR.md) và [benchmark 30 văn bản](docs/OCR_BENCHMARK.md).

```bash
.venv-v2/bin/python -m legal_crawler.v2.export --data data --new-only   # dựng bản nháp vào data/v2_draft/<thời điểm>/, không ghi ES
.venv-v2/bin/python -m legal_crawler.v2.publish --draft data/v2_draft/<thư mục> --data data           # chạy thử, không ghi
.venv-v2/bin/python -m legal_crawler.nightly --once                                                    # crawl -> nháp -> publish (lệnh cron gọi)
```

Lịch: `0 0 * * *` gọi `nightly --once`, nhật ký ở `logs/nightly.log`. Số request LLM song song theo giờ: 3 từ 7h đến 22h, 10 từ 22h đến 7h (`LLM_CCU`, `LLM_CCU_NIGHT`).

**An toàn ghi:**
- Chỉ ghi khi chạy `publish --write` **và** `ES_WRITE_ENABLED=yes` (mặc định `no`: chạy thử).
- Chỉ ghi vào cụm `10.1.10.23`, đúng 3 index `legal-docs`, `legal-chunk-section-v2`, `legal-changed-v2`. Mọi địa chỉ ES của cụm `10.1.10.10` bị mã từ chối, đọc lẫn ghi.
- Chỉ tạo mới, không ghi đè; lỗi thì chỉ xóa những gì lần chạy đó vừa tạo. ID văn bản là số nhỏ nhất còn trống từ `CRAWLER_ID_START` (mặc định 10100000).
- PDF tải lên bucket MinIO riêng `CRAWLER_BUCKET` (mặc định `legal-crawler`).

Cài đặt đầy đủ cần `PyMuPDF`, `minio`, `beautifulsoup4`: `.venv-v2/bin/pip install -r requirements.txt`.

Chuẩn bị test ghi v2 trên cụm 23 bằng `python -m legal_crawler.v2.preflight --data <thư_mục_dữ_liệu> --out preflight.json`; thêm `--check-ocr` để kiểm tra các PDF và `--draft <bản_nháp>` để validation theo mapping thật/chạy thử publisher không ghi. Xem [bộ test, lệnh ghi một văn bản và receipt/hoàn tác](docs/ES2_TEST.md). Export mặc định chỉ kiểm tra bộ index v2; không cần các index v3.
