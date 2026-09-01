from ailive.client.app import natural_sort_key


def test_reference_audio_folders_use_natural_number_order() -> None:
    names = ["10", "2", "1", "赵11", "赵3", "赵20"]
    assert sorted(names, key=natural_sort_key) == ["1", "2", "10", "赵3", "赵11", "赵20"]


def test_reference_audio_paths_use_natural_number_order() -> None:
    paths = ["10/音频2.wav", "2/音频10.wav", "2/音频3.wav", "1/音频1.wav"]
    assert sorted(paths, key=natural_sort_key) == [
        "1/音频1.wav",
        "2/音频3.wav",
        "2/音频10.wav",
        "10/音频2.wav",
    ]
