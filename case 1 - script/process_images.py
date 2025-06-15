#!/usr/bin/env python3
import json
import os
from argparse import ArgumentParser
from datetime import timedelta
from io import BytesIO
from multiprocessing import cpu_count
from multiprocessing.pool import ThreadPool
from typing import Tuple

import numpy as np
import requests
from PIL import Image
from tqdm import tqdm


def main():
    parser = ArgumentParser(
        description="Tool to send images for classification with some "
        "preprocessing (rotate 90 and resize twice)"
    )
    parser.add_argument(
        "--input-dir", type=str, help="directory with the images to classify"
    )
    parser.add_argument("url", type=str, help="classification api url")
    parser.add_argument(
        "--skip-rotation",
        action="store_true",
        help="set this to skip images rotation before classification",
    )
    parser.add_argument(
        "--skip-resize",
        action="store_true",
        help="set this to skip images resize before classification",
    )
    parser.add_argument(
        "--threads-num",
        type=int,
        default=cpu_count(),
        help="number of threads to use to send images",
    )
    args = parser.parse_args()

    images = []
    for image_fn in os.listdir(args.input_dir):
        image_path = args.input_dir + "/" + image_fn
        if image_path[-4:] != ".jpg" or image_path[-4:] != ".png":
            print(f"{image_path} is not an image")
            continue
        images.append(image_path)

    pool = ThreadPool(args.threads_num)
    results = {}
    elapsed_stats = []
    image_size_stats = []
    class_count = {}
    for res in pool.imap_unordered(
        lambda x: process_image(x, results, args), tqdm(images)
    ):
        elapsed, cls, image_size = res
        if elapsed is None:
            continue

        send_stats(elapsed, cls, image_size)

        if cls not in class_count:
            class_count[cls] = 0
        class_count[cls] = class_count[cls] + 1

        elapsed_stats.append(elapsed)
        image_size_stats.append(image_size)

    f = open("output.json", "w")
    json.dump(results, f, indent=2)

    print("Classification results:")
    for k in class_count:
        print(k, "--", class_count[k])

    elapsed_stats = sorted(elapsed_stats)
    image_size_stats = sorted(image_size_stats)
    print(
        "Request time min/avg/max: {}/{}/{}".format(
            elapsed_stats[0],
            sum(elapsed_stats) / len(elapsed_stats),
            elapsed_stats[-1],
        )
    )
    print(
        "Image size min/avg/max: {}/{}/{}".format(
            image_size_stats[0],
            sum(image_size_stats) / len(image_size_stats),
            image_size_stats[-1],
        )
    )


def process_image(imp, res, args) -> Tuple[timedelta, str, float, int]:
    try:
        im = Image.open(imp)

        if args.skip_rotation:
            width, height = im.size
            im = im.resize((width / 2, height / 2))
        elif args.skip_resize:
            im = im.transpose(Image.ROTATE_90)
        else:
            width, height = im.size
            im = im.transpose(Image.ROTATE_90)
            im = im.resize((width / 2, height / 2))

        magic_number = calc_magic_number(np.array(im))

        temp = BytesIO()
        im.save(temp, format="jpg")
        image_jpeg_code = temp.getvalue()

        r = requests.post(
            args.url + "/api/classify",
            headers={"Content-Type": "image/jpeg"},
            params={"magic": magic_number},
            data=image_jpeg_code,
        )

        res[imp] = {
            "elapsed": r.elapsed.total_seconds(),
            "class": r.json().get("class"),
            "score": r.json().get("score"),
        }

        return (
            r.elapsed,
            r.json().get("class"),
            r.json().get("score"),
            len(image_jpeg_code),
        )
    except:
        print("Connection to classification API failed")
        return None, None, None


def calc_magic_number(image):
    result = 0
    for x in range(image.shape[0]):
        for y in range(image.shape[1]):
            result += (x + 1) * (y + 1) * image[x, y]
    return result / (image.shape[0] * image.shape[1])


# Реализация этой функции опущена для простоты. Можете считать, что она всегда
# завершается успешно и не выкидывает исключений, а внутри использует
# библиотеку requests для отправки HTTP запроса.
def send_stats(elapsed, cls, image_size):
    pass


if __name__ == "__main__":
    main()
