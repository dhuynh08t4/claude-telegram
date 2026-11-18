# Claude Code & Antigravity qua Telegram (mỗi Topic = một dự án)

Điều khiển coding agents (Claude Code và Antigravity CLI) từ xa qua Telegram Forum Topics. Mỗi topic tương ứng với một workspace/dự án độc lập.

## Cài đặt
1. @BotFather → `/newbot` → lấy token.
2. Tạo Supergroup → Settings → bật **Topics**. Thêm bot vào nhóm, cấp **Admin** với quyền **Manage Topics**.
3. `cp .env.example .env` rồi điền `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS` (lấy qua @userinfobot), `PROJECTS_ROOT`.
4. Cài thư viện:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
5. Chạy thử: `.venv/bin/python bot.py`
6. Chạy nền bằng systemd:
   ```bash
   sudo cp claude-telegram.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now claude-telegram
   journalctl -u claude-telegram -f
   ```

## Sử dụng
- Ở General:
  - `/new`: Tạo thư mục mới + topic mới (hỗ trợ nhập tên và chọn engine Claude / Antigravity qua nút bấm).
  - `/open`: Chọn dự án có sẵn theo danh sách phân trang → gắn vào topic.
- Trong topic:
  - Nhắn tin yêu cầu bình thường, bot sẽ thực thi agent trong thư mục dự án và gửi kết quả về.
  - `/engine`: Chuyển đổi qua lại giữa engine Claude Code và Antigravity.
  - `/mode`: Đổi chế độ quyền (`default`, `acceptEdits`, `plan`, `bypassPermissions`).
  - `/stop`: Dừng tác vụ đang chạy.
  - `/reset`: Bắt đầu phiên làm việc mới (giữ nguyên dự án).
  - `/info`: Xem thông tin topic hiện tại.

## Duyệt quyền
Khi agent cần chạy lệnh shell hoặc sửa file chưa có quyền, bot gửi tin nhắn 🔐 kèm các nút:
- ✅ **Cho phép**: chỉ cấp quyền cho lần chạy này.
- ♾ **Luôn cho phép**: lưu rule vào cấu hình để không hỏi lại lần sau.
- ❌ **Từ chối**: hủy lệnh.
- Hoặc **nhắn tin trực tiếp** để từ chối kèm phản hồi/hướng dẫn cụ thể (ví dụ: "dùng pnpm thay vì npm").

Nếu không tương tác trong `APPROVAL_TIMEOUT_SECONDS` (mặc định 15 phút), bot sẽ tự động từ chối.

