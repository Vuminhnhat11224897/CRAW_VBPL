# ADR 0002 — Chốt OCR và chuẩn bị test ES v2 trên cụm 23

- Trạng thái: Chấp nhận cho luồng OCR; chuẩn bị test ghi, chưa thực hiện ghi thật.
- Ngày: 20/10/2026.
- Thay thế các mô tả đích ghi v3, ID cũ và quy tắc ngày trái ngược trong ADR 0001.

## Quyết định

1. Số hiệu/ngày ban hành đọc từ chính PDF và phải khớp chính xác metadata nguồn. Khi thiếu/sai, OCR góc trái/phải rồi dải hẹp. Chỉ sửa hai trường định danh có bằng chứng khớp; giữ OCR thô. File phụ lục thiếu văn bản chính có thể được bỏ để thử PDF tiếp theo trong cùng hồ sơ; lỗi số/ngày hoặc nội dung không được bỏ qua như vậy.
2. Nội dung đặt mục tiêu 95%. Thiếu/thêm trên 5% token so với lớp chữ tạo cảnh báo. Kết quả benchmark là mức khớp với bản đối chiếu, không chứng nhận độ đúng mọi scan/bảng. Không tự ghép các bản chia vùng vào thân bài. Ngày ký chưa xác minh giữ trống, không suy từ dấu đăng tải.
3. Đích ghi là **schema/index v2 trên ES `10.1.10.23:9200`**: `legal-docs`, `legal-chunk-section-v2`, `legal-changed-v2`. Quan hệ mới đã có bước QA và validation. Không phụ thuộc mapping/index v3 để export mặc định. Cụm ES 10 vẫn bị cấm; không đổi guard để chạy test.
4. ID theo allocator hiện hành: số nhỏ nhất còn trống từ `CRAWLER_ID_START`, mặc định `10100000`, không vượt dải integer v2. Kiểm cả document/chunk/quan hệ, giữ bảng ID cục bộ và kiểm lại trước ghi; không ghi đè dữ liệu cũ.
5. Mặc định không ghi. Publish cần `--write` và `ES_WRITE_ENABLED=yes`; vector 1024 chiều, chunk, checksum PDF, provenance và mapping phải hợp lệ. MinIO dùng bucket riêng, từ chối `legal-upload`.
6. Preflight chỉ đọc ES/MinIO, kiểm dịch vụ và dữ liệu, có thể chạy OCR kiểm đầu vào; lỗi kết nối hoặc thiếu mapping thì báo chưa sẵn sàng. Không dùng mapping/danh mục giả để tạo một lượt test thành công. Receipt chỉ liệt kê ID đã tạo đối với trạng thái `written`.

## Xác nhận hiện có

- OCR benchmark 30 PDF, 67 trang nội dung mẫu: [kết quả](../OCR_BENCHMARK.md).
- Chuẩn bị bốn hồ sơ thật/năm PDF trong bộ test riêng; hai ca qua OCR, hai ca giữ kiểm tra nội dung. LLM `/models`, OCR và sinh thử embedding hoạt động; ES/MinIO timeout từ máy hiện tại.
- Chưa dựng draft với mapping sống hoặc thực hiện ghi/đọc lại ES thật. [Quy trình test và điều kiện đi tiếp](../ES2_TEST.md).
