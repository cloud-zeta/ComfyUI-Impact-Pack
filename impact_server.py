import os
import threading

from aiohttp import web
import server
import folder_paths

import impact_core as core
import impact_pack
from segment_anything import SamPredictor, sam_model_registry
import numpy as np
import nodes
import torch
from PIL import Image
import io

@server.PromptServer.instance.routes.post("/upload/temp")
async def upload_image(request):
    upload_dir = folder_paths.get_temp_directory()

    if not os.path.exists(upload_dir):
        os.makedirs(upload_dir)
    
    post = await request.post()
    image = post.get("image")

    if image and image.file:
        filename = image.filename
        if not filename:
            return web.Response(status=400)

        split = os.path.splitext(filename)
        i = 1
        while os.path.exists(os.path.join(upload_dir, filename)):
            filename = f"{split[0]} ({i}){split[1]}"
            i += 1

        filepath = os.path.join(upload_dir, filename)

        with open(filepath, "wb") as f:
            f.write(image.file.read())
        
        return web.json_response({"name": filename})
    else:
        return web.Response(status=400)


sam_predictor = None
default_sam_model_name = os.path.join(impact_pack.model_path, "sams", "sam_vit_b_01ec64.pth")

sam_lock = threading.Condition()

last_prepare_data = None

@server.PromptServer.instance.routes.post("/sam/prepare")
async def load_sam_model(request):
    global sam_predictor
    global last_prepare_data
    data = await request.json()

    with sam_lock:
        if last_prepare_data is not None and last_prepare_data == data:
            # already loaded: skip -- prevent redundant loading
            return web.Response(status=200)

        last_prepare_data = data

        model_name = os.path.join(impact_pack.model_path, "sams", data['sam_model_name'])

        print(f"ComfyUI-Impact-Pack: Loading SAM model '{impact_pack.model_path}'")

        filename, image_dir = folder_paths.annotated_filepath(data["filename"])

        if image_dir is None:
            typ = data['type'] if data['type'] != '' else 'output'
            image_dir = folder_paths.get_directory_by_type(typ)

        if image_dir is None:
            return web.Response(status=400)

        if 'vit_h' in model_name:
            model_kind = 'vit_h'
        elif 'vit_l' in model_name:
            model_kind = 'vit_l'
        else:
            model_kind = 'vit_b'

        sam_model = sam_model_registry[model_kind](checkpoint=model_name)
        sam_predictor = SamPredictor(sam_model)

        image_path = os.path.join(image_dir, filename)
        image = nodes.LoadImage().load_image(image_path)[0]
        image = np.clip(255. * image.cpu().numpy().squeeze(), 0, 255).astype(np.uint8)

        sam_predictor.set_image(image, "RGB")


@server.PromptServer.instance.routes.post("/sam/release")
async def release_sam(request):
    global sam_predictor

    with sam_lock:
        sam_predictor = None

    print(f"ComfyUI-Impact-Pack: unloading SAM model")


@server.PromptServer.instance.routes.post("/sam/detect")
async def sam_detect(request):
    global sam_predictor
    with sam_lock:
        if sam_predictor is not None:
            data = await request.json()

            positive_points = data['positive_points']
            negative_points = data['negative_points']
            threshold = data['threshold']

            points = []
            plabs = []

            for p in positive_points:
                points.append(p)
                plabs.append(1)

            for p in negative_points:
                points.append(p)
                plabs.append(0)

            detected_masks = core.sam_predict(sam_predictor, points, plabs, None, threshold)
            mask = core.combine_masks2(detected_masks)

            if mask is None:
                return web.Response(status=400)

            image = mask.reshape((-1, 1, mask.shape[-2], mask.shape[-1])).movedim(1, -1).expand(-1, -1, -1, 3)
            i = 255. * image.cpu().numpy()

            img = Image.fromarray(np.clip(i[0], 0, 255).astype(np.uint8))

            img_buffer = io.BytesIO()
            img.save(img_buffer, format='png')

            headers = {'Content-Type': 'image/png'}

            return web.Response(body=img_buffer.getvalue(), headers=headers)

        else:
            return web.Response(status=400)