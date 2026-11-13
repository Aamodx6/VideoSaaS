import torch
from torch.utils.data import DataLoader
from models import MultimodalSentimentModel, MultimodalTrainer


def test_logging():
    # compute_class_weights() expects dict samples with 'emotion_label' /
    # 'sentiment_label' (as MELDDataset returns), not a namedtuple.
    # Use 7 samples so every emotion class (0-6) and every sentiment
    # class (0-2) is present, otherwise 1.0 / 0 gives inf weights.
    samples = []
    for emo in range(7):
        samples.append({
            'text_inputs': {
                'input_ids': torch.ones(128, dtype=torch.long),
                'attention_mask': torch.ones(128, dtype=torch.long),
            },
            'video_frames': torch.ones(30, 3, 224, 224),
            'audio_features': torch.ones(1, 64, 300),
            'emotion_label': torch.tensor(emo),
            'sentiment_label': torch.tensor(emo % 3),
        })
    mock_loader = DataLoader(samples)

    model = MultimodalSentimentModel()
    trainer = MultimodalTrainer(model, mock_loader, mock_loader)

    train_losses = {
        'total': 2.5,
        'emotion': 1.0,
        'sentiment': 1.5
    }

    trainer.log_metrics(train_losses, phase="train")

    val_losses = {
        'total': 1.5,
        'emotion': 0.5,
        'sentiment': 1.0
    }
    val_metrics = {
        'emotion_precision': 0.65,
        'emotion_accuracy': 0.75,
        'sentiment_precision': 0.85,
        'sentiment_accuracy': 0.95
    }

    trainer.log_metrics(val_losses, val_metrics, phase="val")


if __name__ == "__main__":
    test_logging()
