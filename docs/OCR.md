# Đọc PDF và kiểm tra OCR

Luồng sử dụng dịch vụ Vintern hiện có. Quyết định đọc lớp chữ hay OCR được thực hiện theo từng trang; một trang khó không làm các trang tốt bị OCR lại. `/ocr/raw` cung cấp nội dung, `/ocr/boxes` chỉ phục vụ vị trí ngày ở trang đầu/cuối khi chính các trang đó cần OCR.

## Cách chạy kiểm tra

Cài `requirements.txt` và dùng Python của môi trường đã cài PyMuPDF:

```bash
python -m legal_crawler.v2.ocr_check --data data --limit 30
```

Lệnh đọc SQLite ở chế độ chỉ đọc, chọn các văn bản `downloaded` có PDF, lưu kết quả dưới `data/ocr_checks/<UTC>/`. Không gọi LLM, Elasticsearch hay MinIO. Nếu đang có manifest theo định dạng bộ thử:

```bash
python -m legal_crawler.v2.ocr_check \
  --data data_benchmark_30 \
  --manifest data_benchmark_30/manifest.json \
  --limit 30
```

Mỗi dòng manifest cần `id`, `file` (tương đối với `--data`), `sha256`; có thể kèm `number`, `issued_date`. Có thể chọn thư mục đầu ra bằng `--out`. Exit code `0`: không phát hiện vấn đề; `2`: có lỗi đọc hoặc tài liệu cần kiểm tra. Kết quả gồm `documents.json`, `summary.json`, nội dung đã ghép từng tài liệu và đường dẫn bằng chứng.

## Quy trình đọc

1. Kiểm tra định dạng, PDF mã hóa và giới hạn `REALTIME_MAX_PAGES` trước khi đọc.
2. Giữ các quy tắc đọc lớp chữ của v2, gồm thứ tự đầu trang, ghép dòng, bảng native và loại header lặp. Các điều kiện chọn OCR giữ cùng ngưỡng chữ, ảnh và nhiều cột như luồng cũ.
3. Trang cần OCR được tách riêng rồi gửi `/ocr/raw`; ghép về đúng thứ tự trang. Trang PDF hoàn toàn không có chữ, ảnh hay nét vẽ được ghi nhận là `blank`, không bỏ qua không dấu vết.
4. Với trang cần OCR ở đầu/cuối, gọi `/ocr/boxes` trên PDF một trang và ánh xạ lại số trang gốc. Trang giữa không gọi API này. Trang đầu/cuối có lớp chữ dùng tọa độ native theo quy tắc ngày hiện hành.
5. Cache kết quả API theo SHA-256 thực của PDF, URL OCR, scale, phiên bản cache và cấu hình đọc lại. Lần chạy tiếp theo tái sử dụng kết quả đã hoàn thành; vẫn kiểm tra lại với metadata nguồn hiện tại. Cache hỏng được bỏ qua và gọi lại API.

`text_source` có thể là `text_layer`, `ocr`, `mixed` (có cả lớp chữ và OCR), hoặc `docx`. Báo cáo lưu tổng trang, danh sách trang OCR, phương pháp từng trang, trang cần kiểm tra, thời gian và vị trí bằng chứng. `no_issues_detected` chỉ có nghĩa các kiểm tra không phát hiện vấn đề, không phải nội dung đã được xác minh đúng.

## Trang cần kiểm tra

Các dấu hiệu hiện có:

- Chữ quá ít/rỗng, ký tự lỗi, dòng hoặc đoạn dài lặp nguyên văn.
- Bảng Markdown hoặc công thức trong đầu ra OCR: giữ ảnh để đối chiếu cấu trúc hàng/cột, tử số/mẫu số.
- Lớp chữ có thể đối chiếu nhưng OCR thiếu/thêm trên 5% token hoặc mất các chữ số có sẵn. Ngưỡng nội dung mục tiêu là 95%; đây là tín hiệu so với lớp chữ, chưa chứng nhận độ chính xác thực tế.
- Trang đầu OCR không chứa số hiệu hoặc ngày ban hành từ metadata nguồn.

Đây là tín hiệu nghi vấn; có thể có trường hợp báo thừa. Không suy đoán hay thay chữ như “khi”/“chỉ” theo nghĩa của câu. Chúng cũng không phát hiện được mọi lỗi chữ còn hợp lý hoặc mọi bảng/công thức bị bỏ hoàn toàn.

Với `OCR_REVIEW_RETRY=yes`, mỗi trang nghi vấn chỉ có tối đa một lần đọc kiểm chứng:

- Thiếu số hiệu/ngày: đọc lại vùng đầu trang.
- Bảng có tọa độ native: đọc lại bảng đầu tiên với khoảng đệm.
- Không xác định được vùng đủ chắc chắn: đọc lại nguyên trang ở `OCR_REVIEW_SCALE`.

Các bản đọc kiểm chứng nội dung không được tự động ghép đè vào thân bài hoặc coi là một nguồn xác nhận độc lập. Nếu hai bản đọc nguyên trang khác nhau, báo cáo thêm `verification_disagrees`. Ngoại lệ có giới hạn cho **hai trường định danh ở đầu văn bản**, mô tả ở mục tiếp theo: chỉ sửa khi đọc được chính xác giá trị từ PDF và khớp nguồn. Bản OCR thô luôn giữ trong cache.

## Số hiệu, ngày ban hành và vùng ký

Với record nguồn, PDF phải có số hiệu và ngày ban hành **của chính văn bản**, khớp chính xác với `number` và `issued_date`. Không dùng ngưỡng 95% hoặc so khớp gần đúng cho hai trường này. Dòng số được nhận diện theo nhãn `Số:` trước phần `Căn cứ`; ngày ban hành theo dòng địa danh/ngày. Số/ngày của văn bản viện dẫn, ngày đăng Công báo và dấu SAO Y không thay thế hai trường chính.

Nếu lần đọc đầu không khớp hoặc không đọc được:

1. Số hiệu: đọc 1/4 trên trái, tọa độ chuẩn hóa `(0, 0, 0.5, 0.5)`.
2. Ngày ban hành: đọc 1/4 trên phải, `(0.5, 0, 1, 0.5)`.
3. Nếu vẫn chưa khớp, thu hẹp quanh dòng đầu trang; cuối cùng thử cả nửa trên. Tối đa ba vùng cho mỗi trường chưa xác minh. Không đưa giá trị mong đợi vào prompt OCR.

Vùng cắt được raster hóa riêng để không lộ chữ bên ngoài, render theo `OCR_REGION_SCALE`, phóng vùng 2 lần khi tạo PDF gửi dịch vụ. Điều này giúp mô hình tập trung vào chữ nhỏ, không tạo lại chi tiết đã mất do scan mờ.

Chỉ kết quả đã đọc từ PDF và khớp chính xác nguồn được ghi vào `report.identity`. Nếu OCR đầu sai, `header_repairs` ghi rõ sửa gì và dẫn chứng vùng cắt; chỉ dòng số/ngày đầu văn bản được sửa trong văn bản trả về. Không sửa ngày hoặc số của các căn cứ pháp lý trong thân bài. Cache vẫn giữ bản OCR thô và mọi ảnh/câu trả lời vùng cắt. Đổi record nguồn luôn kiểm tra lại; không sao chép giá trị web vào PDF khi OCR chưa đọc được.

Không khớp, thiếu metadata nguồn, có nhiều ứng viên trong cùng một lần đọc, hoặc file chỉ là phụ lục/quy chuẩn thiếu văn bản chính: `ocr_identity_unverified`, chặn **trước LLM và publish**. Thất bại đọc vùng không làm mất nội dung đã đọc. Ngay cả PDF native cũng phải qua kiểm tra định danh khi có record.

Vùng ký được tìm ở trang có `Nơi nhận`, khối `KT./TM./TL./TUQ.` hoặc `XÁC THỰC VĂN BẢN HỢP NHẤT`, có thể trước một phụ lục dài. Trang scan bắt đầu đọc nửa dưới; nếu không thấy tín hiệu khối ký hoặc OCR trả lại câu hướng dẫn đọc, thử vùng trên rồi cột phải. Trang native dùng tọa độ lớp chữ. Đây là bằng chứng hỗ trợ, **không phải xác minh tên người ký/chữ ký viết tay**.

Ngày ký là trường riêng: chỉ đưa vào kết quả chính khi có `signing_date` tham chiếu riêng và đọc được ngày phù hợp từ vùng ký. Nếu không xác minh được thì để trống; không lấy ngày đăng, ngày SAO Y hay suy từ ngày ban hành. Khi nguồn yêu cầu `signing_date` mà không xác minh được, tài liệu bị chặn. Dấu ngày ký chưa xác minh cũng không được dùng để suy ra ngày hiệu lực.

## Benchmark chia vùng

```bash
python -m legal_crawler.v2.ocr_benchmark \
  --data data_benchmark_30 \
  --manifest data_benchmark_30/manifest.json \
  --out data_ocr_regions_30 \
  --body-samples data_ocr_regions_30/samples.json
```

`--body-samples` là tùy chọn, JSON gồm `number`, `page`, `kind` và `reference_file` nếu có bản đối chiếu. Dùng `kind=native_prose_reference` cho nhóm đối chiếu native trong bảng tổng hợp; các loại khác chỉ lưu chỉ số từng trang. Đường dẫn `reference_file` tính từ thư mục chạy hoặc dùng đường dẫn tuyệt đối. Manifest dùng cùng định dạng `ocr_check`; SHA-256 được kiểm tra trước khi chạy. Mặc định 30 văn bản, hai worker. Muốn chỉ đo đầu văn bản thì bỏ `--body-samples`.

Lệnh đọc mới nguyên trang đầu, các góc đầu trang và vùng ký; với mẫu nội dung, so nguyên trang với chia trái/phải ở trang ngang hoặc trên/dưới ở trang dọc. Kết quả API có cache theo nội dung/cấu hình; quyết định so nguồn và chỉ số được tính lại. Lệnh không sửa nguồn, không gọi LLM/ES/MinIO. Kết quả gồm `headers.json`, `bodies.json`, `benchmark-summary.json`, ảnh và chữ từng vùng. Không tự áp dụng kết quả chia nội dung vào luồng sản xuất.

[Kết quả thử 30 văn bản và 67 trang nội dung](OCR_BENCHMARK.md): số hiệu/ngày được đối chiếu riêng; mức khớp token nội dung không dùng để suy ra độ đúng của bảng hoặc scan không có bản chuẩn.

Trong luồng `prepare`, trang còn vấn đề tạo quarantine `ocr_needs_review` **trước bước bóc metadata/embedding**. Lý do chứa đường dẫn `v2_work/<sha256>/ocr-review.json`; tại đó có danh sách trang, vấn đề, ảnh, nội dung lần đầu và bản kiểm chứng. Tài liệu chưa được đưa vào bản nháp để publish; việc duyệt hoặc chỉnh nội dung là bước xử lý riêng, chưa có giao diện duyệt trong repository này. `ocr_needs_review` không nằm trong danh sách loại vĩnh viễn: lần sau đọc lại cache và đánh giá lại, không mất tài liệu.

Cấu hình OCR sai tạo `ocr_config_invalid` để có thể thử lại sau khi sửa cấu hình, không loại vĩnh viễn tài liệu. Tài liệu native không bị ảnh hưởng bởi cấu hình OCR chưa dùng. Nếu API không dùng được, lý do vẫn là `ocr_unavailable`; file bị API từ chối có lý do `ocr_failed`. Một request kiểm chứng tùy chọn thất bại vẫn giữ bằng chứng của lần đầu và trạng thái cần kiểm tra. Không bỏ trang scan khỏi nội dung để tiếp tục.

## Cấu hình

| Biến | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `OCR_API_URL` | `http://127.0.0.1:8000` | Dịch vụ Vintern |
| `OCR_PDF_SCALE` | `2.0` | Scale render PDF, khoảng 0.5–4.0 |
| `OCR_TIMEOUT` | `900` | Timeout đọc API, giây |
| `OCR_WORKERS` | `2` | Số HTTP request OCR đồng thời trong một tiến trình exporter |
| `OCR_REVIEW_RETRY` | `yes` | Có thực hiện một lần đọc kiểm chứng cho trang nghi vấn |
| `OCR_REVIEW_SCALE` | `3.0` | Scale đọc kiểm chứng, khoảng 0.5–4.0 |
| `OCR_REGION_SCALE` | `3.0` | Scale render các góc, dòng định danh và vùng ký; crop được phóng 2 lần |
| `OCR_CACHE_VERSION` | `1` | Tăng giá trị khi server đổi model/prompt tại cùng URL để bỏ cache cũ |

Các thao tác MuPDF của luồng đọc được bảo vệ bằng khóa dùng chung; thời gian chờ HTTP nằm ngoài khóa này. Giới hạn OCR đồng thời áp dụng giữa các thread trong một tiến trình, không điều phối giữa nhiều tiến trình/máy. Khi có nhiều exporter, cần chia mức này theo tài nguyên dùng chung. `max_px` của server hiện tại chỉ áp dụng cho file ảnh; độ phân giải PDF do `pdf_scale` quyết định.

Cache chỉ lưu kết quả API. Các quyết định kiểm tra được tính lại mỗi lần; đổi record nguồn không làm một kết quả cũ tự được duyệt. Bản kiểm chứng có khóa riêng theo vùng cắt; đổi vùng không dùng nhầm kết quả vùng trước. Các file JSON được thay thế nguyên tử để có thể tiếp tục sau lỗi hoặc gián đoạn.

## Kiểm thử

```bash
python -m unittest discover -s tests -v
```

Kiểm thử dùng PDF thật được tạo tại chỗ và mock dịch vụ: tài liệu native, hỗn hợp, scan, trang trắng, thứ tự và tọa độ trang gốc, cache, thay nội dung PDF, lỗi API từng phần, đọc lại vùng, giữ bằng chứng và chốt chặn quarantine. Kiểm tra đồng thời xác nhận giới hạn OCR được giữ giữa các thread. Chất lượng chữ trên tài liệu thật vẫn cần bản đối chiếu độc lập; kiểm thử luồng không thay thế phép đo đó.

## Xác nhận trên dữ liệu đã crawl (20/10/2026)

Kiểm tra lại cách chọn trang trên 30 PDF, tổng 873 trang, so với mã trước thay đổi:

| Công việc | Trước | Sau |
| --- | ---: | ---: |
| Trang gửi đọc chữ OCR | 801 | 570 |
| Trang gửi đọc tọa độ OCR | 801 | 26 |

Đây là số **trang xử lý**, không phải số HTTP request hoặc phép đo tốc độ. Cách chọn trang/ghép nội dung được kiểm tra bằng phản hồi OCR lưu từ lượt thử trước; không chạy lại mô hình trên cả 30 PDF và không đo lại độ chính xác chữ. Sáu PDF vốn đọc hoàn toàn từ lớp chữ cho kết quả giống nguyên văn mã cũ. Cách đếm trong bộ thử ban đầu từng đưa cả dòng trống vào điều kiện nhiều cột, làm đánh dấu thừa 11 trang; bản kiểm tra này sử dụng đúng các dòng có chữ như luồng production.

Đánh giá lại các tín hiệu trên phản hồi đã lưu cho thấy 196 trang thuộc 20 văn bản cần kiểm tra (chủ yếu bảng/công thức). Đây là danh sách nghi vấn, không phải kết luận tất cả các trang đó sai. Chốt chặn trước bước LLM có thể làm số văn bản đi tiếp giảm; những tài liệu này được giữ trong quarantine để xử lý tiếp.

Một lượt gọi dịch vụ thật trên ba PDF gồm 390/2026/NĐ-CP (scan, 5 trang), 113/2026/VBHN-TT-NHNN (native, 6 trang), 52/2026/VBHN-TT-BTC (hỗn hợp, 8 trang) xử lý đủ 19 trang, chỉ OCR 7 trang, không lỗi dịch vụ. Hai trang bảng của bản hỗn hợp có ảnh và bản đọc lại vùng; nội dung lần đầu được giữ nguyên. Chạy lại cùng đầu vào với mạng bị cấm trong phép thử vẫn đọc được hoàn toàn từ cache và giữ nguyên trạng thái cần kiểm tra.

Những kiểm tra này xác nhận luồng, giảm khối lượng OCR và khả năng tiếp tục; chúng không chứng minh Vintern đã hết lỗi như `liệu` thành `dược` hoặc `khi` thành `chỉ`.
