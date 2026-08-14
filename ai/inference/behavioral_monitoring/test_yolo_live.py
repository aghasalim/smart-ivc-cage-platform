from ultralytics import YOLO
import cv2
import sys

MODEL_PATH = "outputs/mouse_detector_best.pt"
CONF_THRESHOLD = 0.25
CAMERA_INDEX = 0

model = YOLO(MODEL_PATH)
print(f"Model loaded: {MODEL_PATH}")

cap = cv2.VideoCapture(CAMERA_INDEX)
if not cap.isOpened():
    print(f"ERROR: Cannot open camera {CAMERA_INDEX}")
    sys.exit(1)

print("Running — press Q to quit")

while True:
    ret, frame = cap.read()
    if not ret:
        print("ERROR: Cannot read frame")
        break

    results = model(frame, conf=CONF_THRESHOLD, verbose=False)
    annotated = results[0].plot()

    n = len(results[0].boxes)
    color = (0, 255, 0) if n > 0 else (0, 0, 255)
    cv2.putText(annotated, f'Mice detected: {n}', (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1, color, 2)

    if n > 0:
        for i, box in enumerate(results[0].boxes):
            conf = float(box.conf[0])
            cv2.putText(annotated, f'M{i+1}: {conf:.2f}',
                       (10, 60 + i*25),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 1)

    cv2.imshow('YOLOv8 Mouse Detector — press Q to quit', annotated)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
print("Done")
