# How to use ML Api

## Docker

ML Api uses Docker to manage all the models APIs. The first step to setup is to make sure  Docker is installed. Please follow the official guide on how to do this: [Install Docker Engine](https://docs.docker.com/engine/install/).

## Model

An Machine Learning model must be prepared in order for it to be run as part of ML Api. An `MLController` must be created using [fastmlapi](https://pypi.org/project/fastmlapi/) package. This wraps the model with an HTTP server.

For the purpose of this guide, I'm going to assume the `MLController` for ML model is defined as a class in a new `ml_server.py` file. (i.e.)

```python
from fastmlapi import MLController, preprocessing, postprocessing

  
class MyMLServer(MLController):
	...
	
	@preprocessing  
	def preprocess(self, X):  
		...
	
	@postprocessing  
	def postprocess(self, y):
		...

if __name__ == "__main__":  
    MyMLServer().run()
```

Please note that the line:
```python
MyMLServer().run()
``` 

automatically runs the ML server on a default `8000` port.

---

Then, a Dockerfile has to be created (named: `Dockerfile`, no extension). 
Please read the official documentation for more details about creating Dockerfiles and what it even is: [FastAPI in Containers - Docker](https://fastapi.tiangolo.com/deployment/docker/)

The Dockerfile defines how to setup docker container with the model's `MLController`. For example, if the `MLController` class is defined in the `ml_server.py` file, the example Docker file may look like that:

```Dockerfile
FROM python:3.11

WORKDIR /code

COPY ./requirements.txt /code/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

COPY ./trained_models/model.pth /code/trained_models/model.pth
COPY ./Model.py /code/Model.py
COPY ./ml_server.py /code/ml_server.py


CMD ["python", "ml_server.py"]
```

Please note that the lines:
```Dockerfile
COPY ./trained_models/model.pth /code/trained_models/model.pth  
COPY ./Model.py /code/Model.py
COPY ./ml_server.py /code/ml_server.py
```

May vary, depending on what is actually needed by the `ml_server.py`. All the necessary files must be copied into the WORKDIR, but some may be omitted (i.e. no need for a training or data files).

## Running in Docker

When a model is prepared, a docker image must be created, using command, executted from the folder where the Dockerfile is defined:

```shell
docker build -t <new-image-name> .
```

Replace `<new-image-name>` with the name of choice.

Then run the container from newly created image:

```shell
docker run -d --name <container-name> -p 8001:8000 <new-image-name>
```

Please note the port forwarding:
```
-p 8001:8000
```

means that the `MLController` will be available on this PC under the `8001` port, which is internally forwarded to `8000` (which MLController expects by default).

This way we can have multiple models running on the same machine, each internally listening on `8000` port, but we actually hit them using unique `8001`, `8002`, ... ports.

## Stopping the model

To stop model running as a docker container use:

```shell
docker stop <container-name>
```

Model stopped this way is NOT removed, and can quickly be started again, using:

```shell
docker start <container-name>
```

## Testing

To test if the model is running correctly, make an HTTP request to the url:

```
http://0.0.0.0:8001/health
```

Please use the first port specified in the port forwarding in the `docker run` command.

Other endpoints (like `/predict`) are also available, please read [API endpoints](https://github.com/AI-MED-AGH/fast-ML-Api?tab=readme-ov-file#api-endpoints) docs section from `fastmlapi` package.
