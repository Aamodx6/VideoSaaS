import torch
from models import MultimodalSentimentModel
import os
import cv2
import numpy as np
import subprocess
import torchaudio
import whisper
from transformers import AutoTokenizer
import sys
import json
import boto3
import tempfile
import re
import shutil
from urllib.parse import urlsplit

from telemetry import Observation

EMOTION_MAP = {0: "anger", 1: "disgust", 2: "fear",
               3: "joy", 4: "neutral", 5: "sadness", 6: "surprise"}
SENTIMENT_MAP = {0: "negative", 1: "neutral", 2: "positive"}


def install_ffmpeg():
    if shutil.which("ffmpeg"):
        return True
    print("Starting Ffmpeg installation...")

    subprocess.check_call([sys.executable, "-m", "pip",
                          "install", "--upgrade", "pip"])

    subprocess.check_call([sys.executable, "-m", "pip",
                          "install", "--upgrade", "setuptools"])

    try:
        subprocess.check_call([sys.executable, "-m", "pip",
                               "install", "ffmpeg-python"])
        print("Installed ffmpeg-python successfully")
    except subprocess.CalledProcessError as e:
        print("Failed to install ffmpeg-python via pip")

    try:
        subprocess.check_call([
            "wget",
            "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
            "-O", "/tmp/ffmpeg.tar.xz"
        ])

        subprocess.check_call([
            "tar", "-xf", "/tmp/ffmpeg.tar.xz", "-C", "/tmp/"
        ])

        result = subprocess.run(
            ["find", "/tmp", "-name", "ffmpeg", "-type", "f"],
            capture_output=True,
            text=True
        )
        ffmpeg_path = result.stdout.strip()

        subprocess.check_call(["cp", ffmpeg_path, "/usr/local/bin/ffmpeg"])

        subprocess.check_call(["chmod", "+x", "/usr/local/bin/ffmpeg"])

        print("Installed static FFmpeg binary successfully")
    except Exception as e:
        print(f"Failed to install static FFmpeg: {e}")

    try:
        result = subprocess.run(["ffmpeg", "-version"],
                                capture_output=True, text=True, check=True)
        print("FFmpeg version:")
        print(result.stdout)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("FFmpeg installation verification failed")
        return False


class VideoProcessor:
    def process_video(self, video_path):
        cap = cv2.VideoCapture(video_path)
        frames = []

        try:
            if not cap.isOpened():
                raise ValueError(f"Video not found: {video_path}")

            # Try and read first frame to validate video
            ret, frame = cap.read()
            if not ret or frame is None:
                raise ValueError(f"Video not found: {video_path}")

            # Reset index to not skip first frame
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

            while len(frames) < 30 and cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                frame = cv2.resize(frame, (224, 224))
                frame = frame / 255.0
                frames.append(frame)

        except Exception as e:
            raise ValueError(f"Video error: {str(e)}")
        finally:
            cap.release()

        if (len(frames) == 0):
            raise ValueError("No frames could be extracted")

        # Pad or truncate frames
        if len(frames) < 30:
            frames += [np.zeros_like(frames[0])] * (30 - len(frames))
        else:
            frames = frames[:30]

        # Before permute: [frames, height, width, channels]
        # After permute: [frames, channels, height, width]
        return torch.FloatTensor(np.array(frames)).permute(0, 3, 1, 2)


class AudioProcessor:
    def extract_features(self, video_path, max_length=300):
        audio_path = video_path.replace('.mp4', '.wav')

        try:
            subprocess.run([
                'ffmpeg',
                '-i', video_path,
                '-vn',
                '-acodec', 'pcm_s16le',
                '-ar', '16000',
                '-ac', '1',
                audio_path
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            waveform, sample_rate = torchaudio.load(audio_path)

            if sample_rate != 16000:
                resampler = torchaudio.transforms.Resample(sample_rate, 16000)
                waveform = resampler(waveform)

            mel_spectrogram = torchaudio.transforms.MelSpectrogram(
                sample_rate=16000,
                n_mels=64,
                n_fft=1024,
                hop_length=512
            )

            mel_spec = mel_spectrogram(waveform)

            # Normalize
            mel_spec = (mel_spec - mel_spec.mean()) / mel_spec.std().clamp_min(1e-8)

            if mel_spec.size(2) < 300:
                padding = 300 - mel_spec.size(2)
                mel_spec = torch.nn.functional.pad(mel_spec, (0, padding))
            else:
                mel_spec = mel_spec[:, :, :300]

            return mel_spec

        except subprocess.CalledProcessError as e:
            raise ValueError(f"Audio extraction error: {str(e)}")
        except Exception as e:
            raise ValueError(f"Audio error: {str(e)}")
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path)


class VideoUtteranceProcessor:
    def __init__(self):
        self.video_processor = VideoProcessor()
        self.audio_processor = AudioProcessor()

    def extract_segment(self, video_path, start_time, end_time, temp_dir):
        segment_path = os.path.join(
            temp_dir, f"segment_{start_time}_{end_time}.mp4")

        subprocess.run([
            "ffmpeg", "-nostdin", "-loglevel", "error", "-i", video_path,
            "-ss", str(start_time),
            "-to", str(end_time),
            "-c:v", "libx264",
            "-c:a", "aac",
            "-ac", "1",
            "-ar", "16000",
            "-y",
            segment_path
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        if not os.path.exists(segment_path) or os.path.getsize(segment_path) == 0:
            raise ValueError("Segment extraction failed: " + segment_path)

        return segment_path


def download_from_s3(s3_uri):
    s3_client = boto3.client("s3")
    bucket = s3_uri.split("/")[2]
    key = "/".join(s3_uri.split("/")[3:])

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_file:
        local_path = temp_file.name
    try:
        s3_client.download_file(bucket, key, local_path)
    except Exception:
        os.remove(local_path)
        raise
    return local_path


def encode_prediction(prediction, response_content_type):
    if response_content_type == "application/json":
        return json.dumps(prediction)
    raise ValueError(f"Unsupported content type: {response_content_type}")


def model_fn(model_dir):
    if os.getenv("REQUIRE_QUALITY_POLICY", "0") == "1" and not os.getenv("QUALITY_POLICY_PATH"):
        raise ValueError("A calibrated quality policy is required for this deployment")
    policy = None
    if os.getenv("QUALITY_POLICY_PATH"):
        from quality import prepare_policy
        with open(os.environ["QUALITY_POLICY_PATH"], encoding="utf-8") as stream:
            policy = prepare_policy(json.load(stream), os.environ["MODEL_VERSION"])
    # Load the model for inference
    if not install_ffmpeg():
        raise RuntimeError(
            "FFmpeg installation failed - required for inference")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = MultimodalSentimentModel().to(device)

    model_path = os.path.join(model_dir, 'model.pth')
    if not os.path.exists(model_path):
        model_path = os.path.join(model_dir, "model", 'model.pth')
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                "Model file not found in path " + model_path)

    print("Loading model from path: " + model_path)
    model.load_state_dict(torch.load(
        model_path, map_location=device, weights_only=True))
    model.eval()

    return {
        'model': model,
        'tokenizer': AutoTokenizer.from_pretrained('bert-base-uncased'),
        'transcriber': whisper.load_model(
            "base",
            device="cpu" if device.type == "cpu" else device,
        ),
        'device': device,
        'quality_policy': policy,
    }


def request_stats(video_path, observation):
    with observation.stage("frame"):
        capture = cv2.VideoCapture(video_path)
        try:
            frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
            fps = capture.get(cv2.CAP_PROP_FPS)
            if np.isfinite(frames) and frames > 0:
                observation.stats["frame_count"] = int(frames)
                if np.isfinite(fps) and fps > 0:
                    observation.stats["video_duration"] = frames / fps
        finally:
            capture.release()


def transform_fn(model, request_body, content_type, accept, context=None):
    observation = Observation()
    failed = True
    try:
        with observation.stage("input"):
            if (content_type or "").partition(";")[0].strip().lower() != "application/json":
                raise ValueError("Unsupported input content type")
            payload = json.loads(request_body)
            if context is not None and hasattr(context, "get_all_request_header"):
                try:
                    headers = {k.lower(): v for k, v in context.get_all_request_header(0).items()}
                    attributes = headers.get("x-amzn-sagemaker-custom-attributes")
                    if attributes:
                        observation.identify(json.loads(attributes))
                except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                    pass
            uri = urlsplit(payload["video_path"])
            bucket = os.getenv("INFERENCE_BUCKET")
            if uri.scheme != "s3" or not uri.netloc or not uri.path.lstrip("/") or uri.query or uri.fragment:
                raise ValueError("Expected an S3 object URI")
            prefix = "/" + os.getenv("INFERENCE_INPUT_PREFIX", "inference/uploads/")
            if bucket and (uri.netloc != bucket or not uri.path.startswith(prefix)):
                raise ValueError("Video is outside the configured inference input location")
            if bucket:
                upload_key = uri.path[len(prefix):]
                single_key = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.(?:mp4|mov|avi)"
                tenant_key = r"(?:[a-f0-9-]{32,36}/){2}[a-f0-9]{32}\.mp4"
                if not re.fullmatch(f"(?:{single_key}|{tenant_key})", upload_key, re.IGNORECASE):
                    raise ValueError("Expected an opaque video upload key")
            local_path = download_from_s3(payload["video_path"])
        try:
            with tempfile.TemporaryDirectory(prefix="sentiment-") as temp_dir:
                prediction = predict_video(local_path, model, temp_dir, observation)
        finally:
            os.remove(local_path)
        with observation.stage("output"):
            prediction["model_version"] = observation.model_version
            response = encode_prediction(prediction, accept if accept not in (None, "*/*") else "application/json")
        failed = False
        return response, "application/json"
    except Exception:
        observation.error_type = observation.error_type or "internal_error"
        # TorchServe logs exceptions. Suppress input values, paths and subprocess stderr.
        raise RuntimeError(observation.error_type) from None
    finally:
        print(json.dumps(observation.event(failed), separators=(",", ":"), allow_nan=False), flush=True)


def predict_video(video_path, model_dict, temp_dir, observation=None, calibration_sink=None):
    from quality import bleep_ratio, classify, distance, prepare_policy, quality_reason

    observation = observation or Observation(sample_rate=0)
    model = model_dict['model']
    tokenizer = model_dict['tokenizer']
    device = model_dict['device']
    policy = model_dict.get("quality_policy")
    if policy is None and os.getenv("QUALITY_POLICY_PATH"):
        with open(os.environ["QUALITY_POLICY_PATH"], encoding="utf-8") as stream:
            policy = json.load(stream)
        policy = prepare_policy(policy, observation.model_version)
        model_dict["quality_policy"] = policy

    request_stats(video_path, observation)
    with observation.stage("audio"):
        try:
            audio = whisper.load_audio(video_path)
        except RuntimeError:
            if policy:
                observation.stats.update(ood_detected=True, degraded_input=True, has_speech=False)
                return {"status": "out_of_domain", "reason_code": "audio_unavailable", "utterances": []}
            raise
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) if audio.size else 0.0
        observation.stats["audio_loudness"] = float(max(-96.0, 20 * np.log10(max(rms, 1e-5))))
        if policy:
            observation.stats["silence_ratio"] = float(np.mean(np.abs(audio) < 0.003)) if audio.size else 1.0
            observation.stats["bleep_ratio"] = bleep_ratio(audio)
    with observation.stage("text"):
        result = model_dict['transcriber'].transcribe(audio, word_timestamps=True)
        observation.stats["has_speech"] = bool(result["segments"])

    if policy:
        observation.stats.update(quality_frames(video_path))
        scores = [float(np.exp(min(0, s["avg_logprob"]))) for s in result["segments"] if "avg_logprob" in s]
        if scores:
            observation.stats["transcript_confidence"] = float(np.mean(scores))
        reason = ("audio_muted" if not result["segments"] else quality_reason(observation.stats, policy["limits"]))
        observation.stats["degraded_input"] = bool(reason)
        observation.stats["ood_detected"] = bool(reason)
        if reason:
            return {"status": "out_of_domain", "reason_code": reason, "utterances": []}

    utterance_processor = VideoUtteranceProcessor()
    predictions = []

    for segment in result["segments"]:
        segment_path = None
        try:
            with observation.stage("frame"):
                segment_path = utterance_processor.extract_segment(
                    video_path, segment["start"], segment["end"], temp_dir)
                video_frames = utterance_processor.video_processor.process_video(segment_path)
            with observation.stage("audio"):
                audio_features = utterance_processor.audio_processor.extract_features(segment_path)
            with observation.stage("text"):
                text_inputs = tokenizer(segment["text"], padding="max_length", truncation=True,
                                        max_length=128, return_tensors="pt")

            # Get predictions
            with observation.stage("fusion"), torch.inference_mode():
                text_inputs = {k: v.to(device) for k, v in text_inputs.items()}
                video_frames = video_frames.unsqueeze(0).to(device)
                audio_features = audio_features.unsqueeze(0).to(device)
                outputs = model(text_inputs, video_frames, audio_features,
                                return_embedding=observation.sampled or bool(policy) or calibration_sink is not None)
                if calibration_sink is not None:
                    if policy:
                        raise ValueError("Calibration exports require uncalibrated predictions")
                    calibration_sink.append({name: outputs[field][0].cpu().tolist() for name, field in (
                        ("emotion_logits", "emotions"), ("sentiment_logits", "sentiments"),
                        ("embeddings", "embedding"))})
                temperatures = policy["temperature"] if policy else {"emotion": 1, "sentiment": 1}
                emotion_probs = torch.softmax(outputs["emotions"] / temperatures["emotion"], dim=1)[0]
                sentiment_probs = torch.softmax(
                    outputs["sentiments"] / temperatures["sentiment"], dim=1)[0]

                if policy:
                    embedding_distance = distance(outputs["embedding"][0].tolist(), policy["mean"], policy["precision"])
                    observation.stats["embedding_distance"] = embedding_distance
                    status, reason = classify(
                        observation.stats, (emotion_probs.tolist(), sentiment_probs.tolist()),
                        embedding_distance,
                        policy["limits"])
                    if status != "ok":
                        observation.stats["ood_detected"] = True
                        observation.selected = None
                        return {"status": status, "reason_code": reason, "utterances": []}

                emotion_values, emotion_indices = torch.topk(emotion_probs, 3)
                sentiment_values, sentiment_indices = torch.topk(
                    sentiment_probs, 3)
                embedding = outputs["embedding"][0].tolist() if observation.sampled else None
                emotion_values, emotion_indices = emotion_values.tolist(), emotion_indices.tolist()
                sentiment_values, sentiment_indices = sentiment_values.tolist(), sentiment_indices.tolist()

            predictions.append({
                "start_time": segment["start"],
                "end_time": segment["end"],
                "text": segment["text"],
                "emotions": [
                    {"label": EMOTION_MAP[idx], "confidence": conf} for idx, conf in zip(emotion_indices, emotion_values)
                ],
                "sentiments": [
                    {"label": SENTIMENT_MAP[idx], "confidence": conf} for idx, conf in zip(sentiment_indices, sentiment_values)
                ]
            })
            observation.segment(predictions[-1]["emotions"][0], predictions[-1]["sentiments"][0], embedding)

        except Exception:
            observation.segment_errors += 1
            observation.error_type = observation.error_type or "segment_error"

        finally:
            # Cleanup
            if segment_path is not None and os.path.exists(segment_path):
                os.remove(segment_path)
    if result["segments"] and not predictions:
        raise RuntimeError("Inference failed for all transcribed segments")
    return {"utterances": predictions}


def quality_frames(video_path):
    capture = cv2.VideoCapture(video_path)
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    blur = []
    faces = 0
    try:
        frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        if not np.isfinite(frames) or frames < 1:
            return {}
        for index in np.linspace(0, int(frames) - 1, min(int(frames), 8), dtype=int):
            capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
            ok, frame = capture.read()
            if not ok:
                continue
            width = min(frame.shape[1], 480)
            frame = cv2.resize(frame, (width, max(1, int(frame.shape[0] * width / frame.shape[1]))))
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            blur.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
            if not detector.empty():
                faces += int(len(detector.detectMultiScale(gray, minNeighbors=4)) > 0)
    finally:
        capture.release()
    if not blur:
        return {}
    stats = {"blur_score": float(np.median(blur))}
    if not detector.empty():
        stats["face_rate"] = faces / len(blur)
    return stats




# def process_local_video(video_path, model_dir="model_normalized"):
#     model_dict = model_fn(model_dir)

#     input_data = {'video_path': video_path}

#     predictions = predict_fn(input_data, model_dict)

#     for utterance in predictions["utterances"]:
#         print("\nUtterance:")
#         print(f"""Start: {utterance['start_time']}s, End: {
#               utterance['end_time']}s""")
#         print(f"Text: {utterance['text']}")
#         print("\n Top Emotions:")
#         for emotion in utterance['emotions']:
#             print(f"{emotion['label']}: {emotion['confidence']:.2f}")
#         print("\n Top Sentiments:")
#         for sentiment in utterance['sentiments']:
#             print(f"{sentiment['label']}: {sentiment['confidence']:.2f}")
#         print("-"*50)


# if __name__ == "__main__":
#     process_local_video("./dia2_utt3.mp4")
