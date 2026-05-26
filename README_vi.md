
# Cambridge PDF → Moodle v7_full_fix6 (Reading + Listening, Llama-first)

Bản này lấy nền từ `fix10`, đổi nhánh thành `v7_full_fix6`, và chuyển sang hướng **Llama-first cho cả Reading lẫn Listening**.

## Có gì mới trong v7_full
- giữ nguyên nền preview/editor/snapshot ổn định từ fix10
- thêm chọn **Kỹ năng = Reading / Listening** ngay trong UI
- thêm preset **Llama cho toàn bộ parse**
- khi dùng preset này:
  - Reading: parse `question source + passage text + answer key` bằng LlamaParse
  - Listening: parse `question source + answer key + audioscript` bằng LlamaParse
- Listening có pipeline prepare riêng:
  - tìm `Section 1-4`
  - parse `question source`
  - parse `answer key`
  - parse `audioscripts`
- REVIEW của Listening có thêm:
  - `Audioscript raw`
  - `Audioscript clean`
- export Listening sẽ đưa transcript vào **General feedback** để xem lại sau khi làm bài
- snapshot vẫn giữ nguyên để mở lại preview/review cũ mà không tốn quota API

## Cài đặt

```bash
pip install -r requirements.txt
```

## Chạy

```bash
streamlit run app.py
```

## Hướng dẫn nhanh

### Reading
1. Chọn `Kỹ năng = Reading`
2. Giữ `Parser preset = Llama cho toàn bộ parse`
3. Bấm `Prepare`
4. Vào `REVIEW` để sửa group type / answer key / question markup / passage text
5. Bấm `Export`

### Listening
1. Chọn `Kỹ năng = Listening`
2. Giữ `Parser preset = Llama cho toàn bộ parse`
3. Bấm `Prepare`
4. Trong `REVIEW`, kiểm tra:
   - `Question source raw`
   - `Question markup`
   - `Audioscript raw`
   - `Audioscript clean`
5. Có thể sửa tay transcript nếu parse chưa sạch
6. Bấm `Export`

## Lưu ý cho Listening bản đầu
- tối ưu nhất cho Cambridge chuẩn có `Section 1-4` và `Audioscripts`
- map / plan / diagram labeling vẫn có thể cần review tay
- nếu transcript hoặc answer key chưa đẹp, ưu tiên dùng LlamaParse
- transcript hiện được đưa vào `General feedback`, không hiện sẵn ở question text trong lúc làm bài

## Snapshot
- Sau mỗi lần Prepare / Apply / Generate explanation, app tự lưu snapshot
- Dùng `Load snapshot cũ` để mở lại mà không phải gọi API lại


## Ghi chú kỹ thuật
- v7_full_fix6 dùng LlamaParse cho **các phần text chính** của Reading/Listening.
- Việc dò cửa sổ test/section/trang vẫn dùng scan header cục bộ để xác định đúng phạm vi trang trước khi gửi nội dung qua LlamaParse.
- Nếu bạn muốn hạ một parser về native/OCR, chuyển `Parser preset` sang `Tùy chỉnh thủ công`.


## Mới trong v7_full_fix6

- Listening detect `Section 1-4` chắc hơn nhờ kết hợp header OCR + native text + question-range hints.
- `audioscript clean` được làm sạch tốt hơn: bỏ header/footer lặp, nối dòng hợp lý, tách section rõ hơn.
- Export tạo thêm manifest portable `.camplus.json` và nhúng manifest vào XML/HTML.
- App có thể import ngược XML / HTML / manifest để mở lại preview/review cũ mà không cần quota API.
- Nếu import từ artifact mà không còn PDF gốc, app sẽ tự fallback sang rich preview từ text/markup đã lưu.


## Ghi chú nhanh cho v7_full_fix6

- Listening mặc định khuyên dùng `Cách export audioscript/transcript = Không nhúng transcript vào General feedback` để phần explanation không bị quá dài.
- Khi export Listening, app sẽ sinh thêm 2 file companion:
  - `listening_transcripts_review.html`
  - `listening_transcripts_review.md`
- Với câu hỏi map/plan/diagram, trong REVIEW có thể:
  - nhập trang ảnh override,
  - upload ảnh override,
  - crop ảnh trực tiếp ngay trong app,
  - chọn vị trí ảnh ở đầu/cuối khối, sau nhãn câu, hoặc sau một dòng chứa từ khóa.
