# voice-to-fhir-healthlake-poc
Speech → structured FHIR → clinician-approved write to HealthLake, built on S3 events and serverless AWS services.
Python CDK proof of concept: clinician audio → S3 → Amazon Transcribe Medical → Comprehend Medical → draft FHIR R4 resources → clinician review (Bootstrap UI) → AWS HealthLake. Synthetic data only.
Disclaimer: Independent project, not affiliated with AWS. Synthetic data only, AI-generated drafts, not medical advice, not production software.
