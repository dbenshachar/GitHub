"""Infrequent Kubernetes CronJob management for the Go router."""
import json
import os
import sys
from .function_kubernetes import KubernetesAPI
from .function_pool_router import PoolCronRouter
from .functions import FunctionSpec


def main():
    value = json.load(sys.stdin)
    api = KubernetesAPI(os.environ.get('FUNCTION_NAMESPACE', 'mini-functions'))
    router = PoolCronRouter(api, image=os.environ['FUNCTION_IMAGE'], router_url=os.environ['FUNCTION_ROUTER_URL'])
    if value['method'] == 'DELETE':
        name = value['path'].removeprefix('/v1/schedules/')
        api.request('DELETE', api.path('cronjobs', name), {'propagationPolicy': 'Foreground'})
        result = {'deleted': name}
    else:
        options = value['body']
        spec = FunctionSpec(**options.pop('spec'))
        result = router.schedule(spec, **options)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
