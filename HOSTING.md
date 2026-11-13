# Hosting and deployment

## Recommended platform

Use **Vercel for the first public Next.js release** and keep model serving and video storage on AWS in Sydney. For an eligible personal, non-commercial demo, Vercel Hobby is free within its limits. Use Pro for a commercial service or team collaboration. Vercel lists Pro from US$20/month, with usage and additional paid seats billed separately. Configure functions in Sydney (`syd1`) where available for the project. [Vercel plans](https://vercel.com/docs/plans) · [Pricing](https://vercel.com/pricing) · [Function regions](https://vercel.com/docs/regions)

For the lowest-cost private presentation, run the frontend locally with SQLite and restore the GPU shortly before a live demo. For a public preview, use a persistent managed PostgreSQL service and limit access until the signup and quota paths are verified. If the team later needs direct access to the private Aurora and Valkey subnets, host Next.js on ECS Fargate in the AWS VPC. The current codebase still needs that hosted integration. [ECS load balancing](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-load-balancing.html)

## Hosted release checklist

1. Change the SaaS Prisma datasource to PostgreSQL and generate a PostgreSQL migration from the schema. The existing SQLite migration is for local development. Apply and test the hosted migration before deploying the web app.
2. Configure `DATABASE_URL`, `AUTH_SECRET`, and `AUTH_URL` in the host’s environment settings. Use the canonical HTTPS origin and keep secrets server-side.
3. Configure a dedicated AWS workload role scoped to the application upload prefix and inference endpoint or async API. On Vercel, integrate its AWS OIDC credential provider with the app’s AWS clients and restrict the role trust policy to the intended project and environment. Verify that the AWS project's managed policies permit that integration. [Vercel AWS OIDC](https://vercel.com/docs/oidc/aws)
4. Set `AWS_REGION=ap-southeast-2`, the deployment’s bucket, upload prefix, and endpoint name. Configure S3 CORS for the actual frontend origin. Keep direct uploads on S3.
5. Start with access restricted to the team. Verify signup, authentication, upload ownership, output, quotas, and duplicate submission handling. Enable public uploads only after adding abuse controls and establishing a spend limit.
6. To offer continuous submission at low traffic, deploy and verify SageMaker Async with a zero-to-one GPU target. Wire the dashboard to submit a job and poll for results. Test the first upload after idle and measure its cold-start time. [SageMaker async autoscaling](https://docs.aws.amazon.com/sagemaker/latest/dg/async-inference-autoscale.html)

## Restore the real-time demo

The real-time endpoint was deleted on 14 November 2026 after the browser demo passed. Live inference requires restoring the endpoint or deploying the async path. The configuration, model registration, and S3 archive were verified after deletion.

The saved deployment uses `sentiment-analysis-endpoint-media-compat` and the model `sentiment-analysis-model-media-compat` in **console dot dream**, Sydney. The serving archive is `s3://aspect-sentiment-lab/inference/sentiment-analysis-model-media-compat/model.tar.gz`.

With a signed-in `agent-toolkit` profile for the project, run these commands when the endpoint is absent:

```powershell
aws sagemaker create-endpoint --profile agent-toolkit --region ap-southeast-2 --endpoint-name sentiment-analysis-endpoint --endpoint-config-name sentiment-analysis-endpoint-media-compat
aws sagemaker wait endpoint-in-service --profile agent-toolkit --region ap-southeast-2 --endpoint-name sentiment-analysis-endpoint
```

Check the endpoint status and run a short browser upload before the presentation. Budget time for instance provisioning and model load. After the demo, remove the endpoint to stop the idle GPU charge. Keep the configuration, model registration, and S3 archive for the next demo. [SageMaker endpoint cleanup](https://docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints-delete-resources.html)

## Cost controls

The Sydney hosting rate previously verified for `ml.g5.xlarge` was US$1.8312 per hour, about US$1,337 over 730 hours before credits and taxes. Keep the Aurora, RDS Proxy, Valkey, and analytics extension off for the demo. Check actual usage and credits in AWS Settings → Billing. Ask the project owner to set the spend limit if the plan supports it. [SageMaker pricing](https://aws.amazon.com/sagemaker/ai/pricing/) · [Detailed cost notes](ai-video-sentiment-model-main/monitoring/COST.md)
