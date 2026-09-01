from ailive.client.network import VoiceSyncWorker


def test_voice_sync_matches_exact_cloud_name_and_text() -> None:
    profile = {
        "name": "本地名称",
        "cloud_name": "稳定云端名称",
        "reference_text": "这是参考文案",
    }
    remote = [
        {
            "reference_id": "voice-1",
            "name": "稳定云端名称",
            "reference_text": "这是参考文案",
        }
    ]

    assert VoiceSyncWorker._matching_remote(profile, remote) == remote[0]


def test_voice_sync_reuses_unique_text_after_folder_or_machine_change() -> None:
    profile = {
        "name": "新文件夹名称",
        "cloud_name": "新机器生成的名称",
        "reference_text": "相同参考文案",
    }
    remote = [
        {
            "reference_id": "voice-old",
            "name": "旧机器名称",
            "reference_text": "相同参考文案",
        }
    ]

    assert VoiceSyncWorker._matching_remote(profile, remote) == remote[0]


def test_voice_sync_does_not_guess_when_text_is_ambiguous() -> None:
    profile = {
        "name": "本地音色",
        "cloud_name": "本地音色-new",
        "reference_text": "重复文案",
    }
    remote = [
        {"reference_id": "voice-1", "name": "甲", "reference_text": "重复文案"},
        {"reference_id": "voice-2", "name": "乙", "reference_text": "重复文案"},
    ]

    assert VoiceSyncWorker._matching_remote(profile, remote) is None
