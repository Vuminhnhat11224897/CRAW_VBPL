# Benchmark đọc vùng OCR trên 30 PDF

Thử trên bộ 30 văn bản đã tải từ Chính phủ/Công báo, tổng 873 trang. Lượt này gọi lại OCR nguyên trang đầu của đủ 30 PDF, đọc các góc đầu trang, vùng ký của 25 văn bản chính và 67 trang nội dung mẫu. Không chạy lại OCR toàn bộ 873 trang. Vẫn dùng dịch vụ Vintern hiện có.

## Số hiệu và ngày ban hành

| Phép kiểm tra | Đọc nguyên trang đầu | Nguyên trang + đọc vùng khi cần |
| --- | ---: | ---: |
| Số hiệu khớp nguồn / 25 PDF có văn bản chính | 24/25 | 25/25 |
| Ngày ban hành khớp nguồn / 25 PDF có văn bản chính | 24/25 | 25/25 |
| Cả hai trường cùng khớp | 23/25 | 25/25 |
| File thiếu văn bản chính, chưa xác minh | 5/30 | 5/30, giữ chặn |

Đây là độ khớp chính xác với metadata nguồn trên bộ thử, không phải lời đảm bảo mô hình không bao giờ đọc sai. Hai ca sửa đã được đối chiếu lại ảnh:

- 153/2026/TT-BTC: nguyên trang và 1/4 trên trái đều đọc `A53`; vùng hẹp quanh dòng `Số:` đọc đúng `153`.
- 55/2026/TT-NHNN: nguyên trang đọc ngày `22/10/2026`; 1/4 trên phải đọc đúng `12/10/2026`.

Năm file thiếu trang ban hành chính: 387/2026/NĐ-CP, 41/2026/TT-BYT, 77/2026/TT-BXD, 39/2026/TT-BYT và 38/2026/TT-BYT. Các số/ngày được dẫn trong phụ lục không được coi là định danh riêng đã xác minh. Cần PDF văn bản chính hoặc bước đối chiếu riêng để đi tiếp.

## Nội dung và ngưỡng 95%

Trong 67 trang mẫu có 31 trang đối chiếu bằng lớp chữ native, 34 trang khó đã chọn ở lượt duyệt trước và hai trang scan 390/2026/NĐ-CP. Chỉ nhóm 31 trang native, tổng 14.250 token, được đưa vào bảng dưới.

| Cách đọc | Sai khác token so với lớp chữ | Mức khớp `1 − WER` | Số trang có WER ≤ 5% |
| --- | ---: | ---: | ---: |
| Nguyên trang | 253/14.250 | 98,22% | 27/31 |
| Chia cứng hai vùng, phóng crop 2 lần | 5.166/14.250 | 63,75% | 16/31 |

Chuẩn hóa NFKC, khoảng trắng, dấu ngoặc kép và ký hiệu Markdown; token theo `\w+`. Edit distance tính cả thay, thêm và bớt token, có phân biệt hoa/thường và dấu. WER từng trang có thể trên 100% khi mô hình sinh thêm nhiều chữ. Không chặn chỉ số về 0–100 để che các trường hợp này.

**98,22% là mức khớp token với lớp chữ của nhóm mẫu, chưa phải độ chính xác được người duyệt chứng nhận cho toàn hệ thống.** Lớp chữ có thể khác thứ tự đọc ở mẫu biểu, chú thích và nhiều cột. Các trang bảng/công thức và scan không có bản chuẩn không được gộp để tạo một tỷ lệ chính xác chung.

Chia vùng có ích ở một số vị trí: khôi phục phần phải của mẫu chứng chỉ 77/2026/TT-BXD trang 4, 10, 14; khôi phục thêm mã NIOSH/OSHA ở 38/2026/TT-BYT trang 31; 44/2026/TT-BNNMT trang 2 giảm 118 sai khác token xuống 3. Tuy nhiên có thể làm đứt câu, cắt nhãn, sinh thêm câu hoặc lặp dài ở phần chú thích. Không ghép tự động các bản chia vùng vào thân bài.

Quyết định áp dụng: giữ nguyên trang cho nội dung mặc định; so với lớp chữ khi có và cảnh báo nếu thiếu/thêm trên 5% token. Các trang bảng/công thức chưa đủ bằng chứng vẫn cần kiểm tra. Định danh luôn dùng khớp chính xác và chặn trường chưa xác minh, không áp dụng ngưỡng 95%.

## Vùng ký và kiểm tra luồng

Đã đọc vùng ký của 25 PDF có văn bản chính. Nhiều khối ký nằm trên nửa trên vì trang cuối ngắn; chỉ đọc nửa dưới có thể gặp nền trắng và mô hình trả lại câu hướng dẫn. Luồng giữ lần đọc dưới, loại câu trả lời dạng hướng dẫn khỏi ứng viên và thử vùng trên/cột phải. Chưa có bộ chép chuẩn ngày ký/tên người ký để tính tỷ lệ chính xác của phần này; ngày ký chưa xác minh giữ trống.

Chạy luồng đọc thật trên 390/2026/NĐ-CP, 153/2026/TT-BTC và 55/2026/TT-NHNN, tổng 18 trang scan: cả ba số hiệu/ngày ban hành khớp nguồn; hai sửa đổi vùng cắt được phản ánh đúng vào đầu văn bản, còn bản OCR thô được giữ lại. 55/2026/TT-NHNN vẫn cần duyệt nội dung trang 4 vì cảnh báo lặp. Không gọi LLM hay publish.

Replay bộ chặn định danh production trên phản hồi đã lưu của đủ 30 PDF cho 25 khớp / 5 chặn do thiếu văn bản chính, không gọi mạng. Chạy lại ba PDF live với HTTP GET/POST bị cấm vẫn thành công từ cache và giữ nguyên kết quả. 129 kiểm thử đều đạt; kiểm tra có cả ngày hiệu lực suy từ ngày ban hành đã sửa, tránh dùng tọa độ OCR cũ còn ngày sai.

## Chạy và xem bằng chứng

Xem lệnh trong [OCR.md](OCR.md#benchmark-chia-vùng). Bộ mẫu nằm trong workspace ở `data_ocr_regions_30/samples.json`; PDF, phản hồi API và ảnh là dữ liệu làm việc bị Git bỏ qua. Chỉ mã thực thi, tài liệu và kiểm thử được đưa vào branch.

- [Báo cáo chi tiết tại workspace](../data_ocr_regions_30/REPORT.md)
- [Kết quả đầu văn bản](../data_ocr_regions_30/headers.json)
- [Kết quả nội dung](../data_ocr_regions_30/bodies.json)
- [Tóm tắt benchmark](../data_ocr_regions_30/benchmark-summary.json)
- [Luồng đọc thật 18 trang](../data_ocr_regions_30/live/documents.json)
