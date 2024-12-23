from flask import Flask, request, jsonify
import face_recognition
import base64
import numpy as np
from io import BytesIO
from PIL import Image
import requests
from datetime import datetime
from geopy.geocoders import Nominatim
import logging
from functools import lru_cache
import time  # Import thêm để đo thời gian
from flask_cors import CORS

# Cấu hình Flask
app = Flask(__name__)
CORS(app, origins=["http://150.95.113.77", "https://ai.nhoytech.site"], supports_credentials=True)


# Cấu hình Strapi
STRAPI_BASE_URL = "http://localhost:1337/api"
STRAPI_UPLOAD_URL = f"{STRAPI_BASE_URL}/upload"
STRAPI_USERS_URL = f"{STRAPI_BASE_URL}/users"
STRAPI_UPDATE_TOKEN = "6bd0541e8b6593aec7184725e98fc463baab86e206b058c709c121fcfe47f8d3c63bb33edf3013bb5cddecd6ae18aa20715753dc757e391a0751c9387635209738ca67806be87ea040bf752ef6bebd36146262c1fade7403767a40aa7752e8f346079c87d060512a6f24a5e0422c627d3c27a35f778a82972accf48c8446409d"

# Cấu hình Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


@lru_cache(maxsize=100)
def get_user_from_strapi(user_id):
    """Lấy thông tin người dùng từ Strapi bằng user_id."""
    try:
        url = f"{STRAPI_USERS_URL}/{user_id}"
        response = requests.get(url)

        if response.status_code == 200:
            user_data = response.json()
            logging.info(f"Lấy thông tin user ID {user_id} thành công.")
            return user_data
        else:
            logging.error(f"Lỗi khi lấy thông tin user ID {user_id}: {response.text}")
            return None
    except Exception as e:
        logging.error(f"Lỗi khi kết nối tới Strapi để lấy thông tin user ID {user_id}: {str(e)}")
        return None


def correct_image_orientation(image):
    """Sửa xoay ảnh dựa trên thông tin EXIF."""
    try:
        exif = image._getexif()
        if exif:
            orientation_key = 274  # Thông tin orientation trong EXIF
            orientation = exif.get(orientation_key)
            if orientation == 3:
                image = image.rotate(180, expand=True)
            elif orientation == 6:
                image = image.rotate(270, expand=True)
            elif orientation == 8:
                image = image.rotate(90, expand=True)
    except AttributeError:
        logging.warning("Ảnh không chứa thông tin EXIF. Bỏ qua sửa xoay.")
    return image


def resize_image(image, target_width=480, target_height=480):
    """Giảm kích thước ảnh để tăng tốc độ xử lý."""
    if image.width > target_width or image.height > target_height:
        aspect_ratio = image.height / image.width

        # Điều chỉnh tỷ lệ theo chiều dài và chiều rộng tối đa
        if aspect_ratio > 1:  # Ảnh đứng (cao hơn rộng)
            new_height = target_height
            new_width = int(target_height / aspect_ratio)
        else:  # Ảnh ngang (rộng hơn cao)
            new_width = target_width
            new_height = int(target_width * aspect_ratio)

        return image.resize((new_width, new_height), Image.Resampling.LANCZOS)
    return image


def compress_image(image, quality=75):
    """
    Nén chất lượng ảnh để giảm kích thước file.
    """
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    return Image.open(buffer)


def crop_face(image, face_location):
    """
    Cắt ảnh chỉ giữ lại khuôn mặt.
    """
    top, right, bottom, left = face_location
    return image.crop((left, top, right, bottom))

def process_image(image, face_location=None, target_width=480, target_height=480, quality=75):
    """
    Tối ưu hóa ảnh bằng cách resize, crop và nén.
    """
    # Resize ảnh trước để giảm kích thước tổng thể
    image = resize_image(image, target_width=target_width, target_height=target_height)

    # Nếu đã có vị trí khuôn mặt, cắt ảnh
    if face_location:
        image = crop_face(image, face_location)

    # Nén ảnh để giảm kích thước file
    image = compress_image(image, quality=quality)

    return image

def upload_image_to_strapi(image_np):
    """Upload ảnh lên Strapi và trả về ID của file."""
    try:
        logging.info("Bắt đầu upload ảnh lên Strapi...")
        image = Image.fromarray(image_np)
        buffer = BytesIO()
        image.save(buffer, format="JPEG")
        buffer.seek(0)

        files = {"files": ("face.jpg", buffer, "image/jpeg")}
        response = requests.post(STRAPI_UPLOAD_URL, files=files)

        if response.status_code == 200:
            uploaded_image = response.json()
            file_id = uploaded_image[0]["id"]
            logging.info(f"Upload ảnh thành công. File ID: {file_id}")
            return file_id
        else:
            logging.error(f"Lỗi upload ảnh: {response.text}")
            return None
    except Exception as e:
        logging.error(f"Lỗi khi upload ảnh: {str(e)}", exc_info=True)
        return None


def update_user_on_strapi(user_id, face_encoding, face_image_id):
    """Cập nhật thông tin user với encoding và ID file ảnh."""
    try:
        logging.info(f"Đang cập nhật thông tin user ID {user_id} trên Strapi...")
        data = {
            "face": face_encoding.tolist(),
            "faceImage": face_image_id,
        }
        response = requests.put(f"{STRAPI_USERS_URL}/{user_id}", json=data)

        if response.status_code == 200:
            logging.info(f"Cập nhật thông tin user ID {user_id} thành công.")
            return True
        else:
            logging.error(f"Lỗi cập nhật user ID {user_id}: {response.text}")
            return False
    except Exception as e:
        logging.error(f"Lỗi khi cập nhật user: {str(e)}", exc_info=True)
        return False


@app.route("/register", methods=["POST"])
def register_face():
    start_time = time.time()  # Đo thời gian bắt đầu
    data = request.get_json()
    if "user_id" not in data or "image" not in data:
        logging.error("Thiếu tham số 'user_id' hoặc 'image' trong request.")
        return jsonify({"success": False, "message": "Thiếu tham số user_id hoặc image"}), 400

    user_id = data["user_id"]
    image_b64 = data["image"]

    try:
        logging.info(f"Đăng ký khuôn mặt cho user ID {user_id}...")
        step_time = time.time()
        image_data = base64.b64decode(image_b64)
        image = Image.open(BytesIO(image_data)).convert("RGB")
        image = resize_image(image)
        image = process_image(image)  # Giảm kích thước ảnh
        image_np = np.array(image)
        logging.info(f"Thời gian xử lý ảnh: {time.time() - step_time:.2f}s")

        step_time = time.time()
        face_locations = face_recognition.face_locations(image_np, model="hog")
        if not face_locations:
            logging.error(f"Ảnh của user ID {user_id} không chứa khuôn mặt hợp lệ.")
            return jsonify({"success": False, "message": "Ảnh không chứa khuôn mặt hợp lệ"}), 400
        logging.info(f"Thời gian phát hiện khuôn mặt: {time.time() - step_time:.2f}s")

        step_time = time.time()
        face_location = face_locations[0]
        face_encodings = face_recognition.face_encodings(image_np, [face_location])
        if not face_encodings:
            logging.error(f"Không thể trích xuất encoding khuôn mặt từ ảnh của user ID {user_id}.")
            return jsonify({"success": False, "message": "Không thể trích xuất dữ liệu khuôn mặt"}), 400
        face_encoding = face_encodings[0]
        logging.info(f"Thời gian trích xuất encoding: {time.time() - step_time:.2f}s")

        step_time = time.time()
        face_image_id = upload_image_to_strapi(image_np)
        if not face_image_id:
            return jsonify({"success": False, "message": "Lỗi khi upload ảnh lên Strapi"}), 500
        logging.info(f"Thời gian upload ảnh: {time.time() - step_time:.2f}s")

        step_time = time.time()
        if update_user_on_strapi(user_id, face_encoding, face_image_id):
            logging.info(f"Đăng ký khuôn mặt cho user ID {user_id} thành công.")
            logging.info(f"Thời gian cập nhật user: {time.time() - step_time:.2f}s")
            logging.info(f"Tổng thời gian đăng ký: {time.time() - start_time:.2f}s")
            return jsonify({"success": True, "message": "Đăng ký khuôn mặt thành công"}), 200
        else:
            logging.error(f"Lỗi khi cập nhật thông tin user ID {user_id} trên Strapi.")
            return jsonify({"success": False, "message": "Lỗi khi cập nhật thông tin user trên Strapi"}), 500

    except Exception as e:
        logging.error(f"Lỗi xử lý ảnh cho user ID {user_id}: {str(e)}", exc_info=True)
        return jsonify({"success": False, "message": f"Lỗi xử lý ảnh: {str(e)}"}), 400

@app.route("/recognize", methods=["POST"])
def recognize_face():
    start_time = time.time()  # Đo thời gian bắt đầu

    # Lấy địa chỉ IP từ request
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    logging.info(f"Địa chỉ IP của thiết bị: {client_ip}")

    data = request.get_json()
    if "user_id" not in data or "image" not in data or "shop_id" not in data:
        logging.error("Thiếu tham số 'user_id', 'image', hoặc 'shop_id' trong request.")
        return jsonify({"success": False, "message": "Thiếu tham số user_id, image hoặc shop_id"}), 400

    user_id = data["user_id"]
    image_b64 = data["image"]
    shop_id = data["shop_id"]
    name = data["name"]

    try:
        logging.info(f"Xác thực khuôn mặt cho user ID {user_id}...")
        step_time = time.time()

        # Lấy thông tin user từ Strapi
        user = get_user_from_strapi(user_id)
        if not user:
            logging.error(f"Không tìm thấy thông tin user ID {user_id}.")
            return jsonify({"success": False, "message": "Không tìm thấy user"}), 404
        logging.info(f"Thời gian lấy thông tin user: {time.time() - step_time:.2f}s")

        step_time = time.time()
        if "face" not in user or not user["face"]:
            logging.error(f"User ID {user_id} chưa có dữ liệu khuôn mặt.")
            return jsonify({"success": False, "message": "User chưa có dữ liệu khuôn mặt"}), 400
        known_encoding = np.array(user["face"])
        logging.info(f"Thời gian xử lý encoding từ Strapi: {time.time() - step_time:.2f}s")

        step_time = time.time()
        image_data = base64.b64decode(image_b64)
        image = Image.open(BytesIO(image_data)).convert("RGB")
        image = correct_image_orientation(image)
        image = resize_image(image)  # Giảm kích thước ảnh
        image_np = np.array(image)
        logging.info(f"Thời gian xử lý ảnh: {time.time() - step_time:.2f}s")

        step_time = time.time()
        face_locations = face_recognition.face_locations(image_np, model="hog")
        if not face_locations:
            logging.error(f"Ảnh của user ID {user_id} không chứa khuôn mặt hợp lệ.")
            return jsonify({"success": False, "message": "Ảnh không chứa khuôn mặt hợp lệ"}), 400
        logging.info(f"Thời gian phát hiện khuôn mặt: {time.time() - step_time:.2f}s")

        step_time = time.time()
        face_location = face_locations[0]
        face_encodings = face_recognition.face_encodings(image_np, [face_location])
        if not face_encodings:
            logging.error(f"Không thể trích xuất encoding từ ảnh của user ID {user_id}.")
            return jsonify({"success": False, "message": "Không thể trích xuất dữ liệu khuôn mặt"}), 400
        face_encoding = face_encodings[0]
        logging.info(f"Thời gian trích xuất encoding: {time.time() - step_time:.2f}s")

        step_time = time.time()
        matches = face_recognition.compare_faces([known_encoding], face_encoding, tolerance=0.6)
        logging.info(f"Thời gian so khớp khuôn mặt: {time.time() - step_time:.2f}s")

        if True in matches:
            current_time = datetime.now().isoformat()  # Thời gian hiện tại

            # Tạo payload để gửi lên custom endpoint
            payload = {
                "checkIn": {
                    "name": name,
                    "time": current_time,
                    "location": client_ip,
                    "ip": client_ip,
                }
            }

            # Gửi thông tin lên custom endpoint Strapi
            strapi_url = f"{STRAPI_BASE_URL}/shops/{shop_id}/add-check-in"
            response = requests.put(strapi_url, json=payload)

            if response.status_code == 200:
                logging.info(f"Cập nhật thông tin check-in cho shop ID {shop_id} thành công.")
            else:
                logging.error(f"Lỗi khi cập nhật thông tin check-in cho shop ID {shop_id}: {response.text}")

            logging.info(f"Xác thực thành công cho user ID {user_id}.")
            logging.info(f"Địa chỉ IP của thiết bị: {client_ip}")
            logging.info(f"Tổng thời gian xác thực: {time.time() - start_time:.2f}s")
            return jsonify({
                "success": True,
                "message": "Xác thực thành công",
                "time": current_time,
                "client_ip": client_ip
            }), 200
        else:
            logging.error(f"Khuôn mặt không khớp với dữ liệu user ID {user_id}.")
            return jsonify({"success": False, "message": "Khuôn mặt không khớp"}), 400

    except Exception as e:
        logging.error(f"Lỗi xử lý ảnh cho user ID {user_id}: {str(e)}", exc_info=True)
        return jsonify({"success": False, "message": f"Lỗi xử lý ảnh: {str(e)}"}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
