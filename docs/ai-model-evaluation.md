# Hermes: AI Model Evaluation (Cost, Security, Ecosystem)

Prepared October 2026 to choose the production AI provider for Hermes. Prices and terms change often: every figure below should be confirmed on the provider's official page, or in a quote, before a decision.

## Summary

- **Cost is not the deciding factor.** At 300+ tickets a week, every realistic option costs between about **$5 and $175 a month**, and most cost under $50. Even if volume tripled, the AI bill would remain small next to hosting and staff time.
- **The deciding factors are data protection and fit:** whether the provider contractually refuses to train on our data, how long it keeps it, where it is processed, whether a data processing agreement (DPA) is in place, and how well it fits the tools SBC already uses.
- **Recommendation:** shortlist **Google Cloud Vertex AI** as the primary option, because SBC already runs on Google Workspace and plans a Google Chat app. Keep **Groq** (current provider, fastest and cheapest) as an alternative if its DPA and US data storage are approved. Hold **self-hosting** in reserve if data must never leave SBC.
- **SBC already uses Gemini under its Google Workspace agreement,** where staff chats are not used to improve Google's models. That protection covers the Gemini app in Workspace, not the developer API Hermes calls; Hermes would get equivalent terms by running through Vertex AI in a Google Cloud project owned by SBC's organisation (section 4).
- **The final choice rests with the business.** This document sets out the evidence; it does not make the decision.
- **Before any real employee data is used:** a signed DPA, a legal review of transferring data outside Nigeria, and a data protection impact assessment (details in section 5).

---

## 1. How much Hermes uses

**Measured on the current system.** A typical email ticket and a third-turn portal chat were each sent to Groq's `gpt-oss-120b`, the model Hermes uses today:

| Request | Input tokens | Output tokens |
|---|---|---|
| Email or Helpdesk ticket triage | 1,024 | 621 |
| Portal chat, third message | 1,029 | 508 |

The input is small today because the knowledge base has only two articles. It will grow as articles are added.

**Volume assumptions**

| Item | Assumption |
|---|---|
| Tickets | 300 a week, about 1,300 a month |
| AI requests per ticket | 1 for an email ticket; about 3 to 4 for a portal chat; occasional re-triage after a refused solution. Planning average: 3 |
| Monthly category review (F2) | 1 request per ticket reviewed, about 1,300 a month |
| **Total** | **about 5,200 requests a month.** The estimates below use **10,000 a month**, nearly double, to stay conservative |

**Three sizes of request**

| Scenario | Input tokens | Output tokens | When |
|---|---|---|---|
| A. Today | 1,000 | 600 | Small knowledge base |
| B. Large knowledge base sent whole | 20,000 | 600 | About 50 articles sent with every request, as Hermes works today |
| C. Large knowledge base with retrieval | 3,000 | 600 | Only the most relevant articles sent (backlog item B5) |

---

## 2. Cost at 10,000 requests a month

Prices are US dollars per million tokens, standard (non-batch) rates.

| Provider and model | Input price | Output price | A. Today | B. Whole KB | C. KB retrieval |
|---|---|---|---|---|---|
| Groq `gpt-oss-120b` (current) | $0.15 | $0.60 | $5 | $34 | $8 |
| Groq `gpt-oss-20b` | $0.075 | $0.30 | $3 | $17 | $4 |
| Google Gemini 2.5 Flash-Lite | $0.10 | $0.40 | $3 | $22 | $5 |
| Anthropic Claude Haiku 5.5 | $0.10 | $0.50 | $4 | $23 | $6 |
| Google Gemini 3.8 Flash (current fallback), until 31 Dec 2026 | $0.75 | $3.75 | $30 | $173 | $45 |
| Google Gemini 3.8 Flash, from 1 Jan 2027 | $1.50 | $7.50 | $60 | $345 | $90 |
| Anthropic Claude Sonnet 5.5 (higher-end comparison) | $2.00 | $10.00 | $80 | $460 | $120 |

Monthly totals, rounded. Notes:
- **Gemini 3.8 Flash doubles in price on 1 January 2027,** according to Google's pricing page. Its output price also covers the model's internal "thinking" tokens, so real output counts can exceed the visible reply.
- **Batch processing** (requests answered within hours instead of seconds) halves the price on Groq, Google and Anthropic. That suits the monthly category review, not live chat.
- **Images** (attachments, backlog B2) add roughly one to two thousand input tokens each on the models that can read them. At this volume that is a few dollars a month.
- **Real-time and portal requests could fall** if Hermes resolves more issues itself, but costs scale in a straight line: halving volume halves the bill.

**What this means:** the cheapest and most expensive realistic choices differ by well under $200 a month. The choice should be made on security, compliance and quality, not price.

---

## 3. Security and data handling

| Provider | Trains on our data? | Retention | Where data is processed | Contract and certifications |
|---|---|---|---|---|
| **Groq** (paid API) | Not stated on Groq's data page; a third-party summary says Groq's DPA prohibits it. Must be confirmed in the contract | Inputs and outputs not kept by default for inference; Zero Data Retention can be switched on in the console | Stored in US Google Cloud buckets; standard contractual clauses offered for transfers | Trust Center referenced; SOC 2 and DPA not confirmed on the public pages. Request both |
| **Gemini via Google AI Studio API, free tier** (current fallback) | **Yes.** Google's pricing page states free-tier content is used to improve its products, and human reviewers may read it | Per Google terms | Google global infrastructure | **Not acceptable for company data** |
| **Gemini via Google AI Studio API, paid tier** | No (pricing page: content not used to improve products) | Per Gemini API terms | Google global infrastructure | Gemini API terms |
| **Google Cloud Vertex AI** (Gemini, also offers Claude) | No. Google Cloud's training restriction covers all managed models without our permission | Up to 24 hours of input caching by default (can be disabled); abuse-monitoring logging, for which an exception can be requested to reach zero retention | Regional endpoints (US, EU and others) with processing kept in region for listed endpoints. A Johannesburg region exists, but Gemini processing there is not confirmed | Google Cloud terms and Cloud Data Processing Addendum; Google Cloud's compliance programme |
| **Anthropic Claude API** | No. The commercial terms state Anthropic may not train on customer content | Sources disagree between 7 and 30 days by default; zero retention available for qualifying enterprise customers | Global routing by default; US-only processing at 10% extra | Commercial terms and DPA. Also available through Vertex AI and Amazon Bedrock under those platforms' terms |
| **Azure OpenAI** | No (Microsoft: prompts and outputs are not used to train or improve the models) | About 30 days for abuse monitoring, with an opt-out process | Depends on deployment type: "Global" can process anywhere, "DataZone" within a zone, regional within a region | Microsoft product terms and DPA. Model pricing and South Africa availability not confirmed in this research |
| **Self-hosted open model** (`gpt-oss-20b`) | No third party involved | Fully under SBC control | On SBC's own server | No external contract; SBC carries all security and maintenance |

**Model capabilities relevant to the backlog**
- **Images and screenshots (B2):** Gemini and Claude models accept images. Groq's public model list does not show a vision model for the models Hermes uses, so attachments would need a second provider if Groq stays.
- **Speech (voice notes):** Groq offers Whisper transcription at $0.04 to $0.11 per hour of audio.

---

## 4. Ecosystem fit

**SBC's existing Google relationship.** SBC staff already use Gemini through the company's Google Workspace, which shows: "Your SEVENUP BOTTLING COMPANY LTD. chats aren't used to improve our models." That commitment comes from the Workspace agreement and applies to the Gemini app used by staff. It does **not** automatically extend to Hermes:

| Way of using Gemini | Covered by SBC's Workspace terms? | Suitable for Hermes? |
|---|---|---|
| Gemini app inside SBC's Google Workspace (what staff use today) | Yes | Not usable by Hermes: it is a chat app for people, not a service Hermes can call |
| Gemini API key from Google AI Studio without billing (Hermes' current fallback, if created this way) | No: free tier, content used to improve Google's products | **No** |
| Gemini API key from Google AI Studio with billing | No, but paid-tier terms apply: content not used to improve products | Acceptable once reviewed |
| **Vertex AI in a Google Cloud project owned by SBC's organisation** | Separate Google Cloud terms, with the training restriction and Cloud Data Processing Addendum | **Yes: the closest match to the protection staff already have** |

Because SBC's Google organisation already exists, setting up a Google Cloud project under it is an administrative step (project, billing account, access control), not a new vendor relationship. Whether SBC's existing Google agreement already includes Google Cloud terms is a question for whoever manages the Workspace contract.

| Option | Fit with SBC | Notes |
|---|---|---|
| **Google Cloud Vertex AI** | **Strongest.** SBC already uses Google Workspace, Hermes' single sign-on will use Google, and the Google Chat app (F3) is built and hosted in Google Cloud | One vendor relationship, one bill, Google Cloud access control. Offers both Gemini and Claude models. Hermes already uses Google's `google-genai` library, which can be pointed at Vertex AI with a small change |
| **Groq** | Good technically; separate vendor | Fastest responses in our tests (2 to 4 seconds against 12 to almost 100 seconds for Gemini's free tier). US data storage. Smaller vendor; no vision model shown for our use |
| **Anthropic direct** | Neutral; separate vendor | Strong instruction following. Available through Vertex AI, which removes the separate-vendor drawback |
| **Azure OpenAI** | Moderate | Natural for the .NET intranet team if SBC has a Microsoft agreement; otherwise a new vendor alongside Google |
| **Self-hosted** | Highest data control, highest effort | Needs a server with a 16 to 24 GB GPU (for example an RTX 4090-class card), plus patching, monitoring and capacity planning. `gpt-oss-20b` handles 300 tickets a week comfortably, but its answers are weaker than the larger hosted models |

---

## 5. Compliance (Nigeria)

SBC's employee data is subject to the **Nigeria Data Protection Act 2023 (NDPA)** and the 2025 General Application and Implementation Directive (GAID). This research is not legal advice; SBC's data protection officer or counsel should confirm the following:

- **Transfers outside Nigeria (sections 41 to 43):** personal data may only go abroad where the destination offers adequate protection (for example through binding contract clauses or a Commission adequacy decision) or another basis in section 43 applies. Every hosted option above processes data outside Nigeria, so the provider's DPA and transfer clauses must be reviewed against this.
- **Data protection impact assessment (section 28):** required where processing is likely to pose a high risk. AI processing of employee support conversations is a strong candidate. Commentary notes that the regulator frequently requests these in enforcement actions.
- **Automated decisions (section 37):** people have the right not to be subject to solely automated decisions with significant effects. Hermes' design already keeps people in the loop: technicians handle escalations, users can refuse a solution, and category changes need human approval.
- **Breach reporting:** breaches likely to cause high risk must be reported to the Commission within 72 hours, so the provider's breach notification terms matter.
- **Data minimisation:** whatever the provider, Hermes should send only what each task needs and mask names, emails and phone numbers where possible (planned for the category review, F2).

---

## 6. Recommendation and next steps

1. **Shortlist two options:** Google Cloud Vertex AI (Gemini Flash-Lite or Flash, or Claude Haiku on Vertex) as the preferred route, and Groq paid with Zero Data Retention as the alternative.
2. **Request documents:** the DPA, the SOC 2 report and the transfer terms from both shortlisted providers; confirm Vertex AI regional processing for the chosen model. Ask SBC's Workspace administrator whether the existing Google agreement already covers Google Cloud.
3. **Legal review:** NDPA transfer basis and a data protection impact assessment, with SBC's data protection officer.
4. **Bake-off on test data:** run the same 50 to 100 anonymised or synthetic tickets through each shortlisted model and compare correct category, correct knowledge base match, valid structured output, reply quality and response time. Hermes already supports switching models, so this is a configuration exercise.
5. **Until all of this is done,** Hermes continues on test data only, and the free-tier Gemini fallback must not receive real tickets.

---

## Sources

- [Groq models and pricing](https://console.groq.com/docs/models)
- [Groq: your data](https://console.groq.com/docs/your-data)
- [Groq Trust Center](https://trust.groq.com)
- [Groq launch post for gpt-oss models](https://groq.com/blog/day-zero-support-for-openai-open-models)
- [Groq privacy summary (third party)](https://meetily.ai/llm-privacy/groq)
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API terms analysis (Simon Willison)](https://simonwillison.net/2024/Oct/17/gemini-terms-of-service/)
- [Gemini pricing overview (CloudZero)](https://www.cloudzero.com/blog/gemini-pricing/)
- [Vertex AI data governance](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/data-governance)
- [Vertex AI data residency](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/data-residency)
- [Vertex AI locations](https://docs.cloud.google.com/vertex-ai/docs/general/locations)
- [Gemini 3.8 Flash on Vertex (Eden AI listing)](https://www.edenai.co/models/gemini-3-8-flash)
- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Anthropic data retention summary (third party)](https://www.getvoibe.com/resources/claude-api-data-retention/)
- [Anthropic privacy summary (third party)](https://meetily.ai/llm-privacy/anthropic)
- [Azure OpenAI data, privacy and security](https://learn.microsoft.com/en-us/legal/cognitive-services/openai/data-privacy)
- [OpenAI API pricing overview (Morph)](https://morphllm.com/openai-api-pricing)
- [gpt-oss-20b hardware requirements](https://intuitionlabs.ai/articles/hardware-requirements-gpt-oss-20b)
- [NDPA cross-border transfers (Mondaq)](https://www.mondaq.com/nigeria/data-protection/1789480/cross-border-data-transfers-an-overview-of-legal-requirements-and-compliance-mechanisms-in-nigeria)
- [NDPA overview (KPMG)](https://assets.kpmg.com/content/dam/kpmg/ng/pdf/nigeria-data-protection-act2023.pdf)
- [NDPA key provisions (Mondaq)](https://www.mondaq.com/nigeria/data-protection/1366934/the-nigerias-data-protection-act-2023-a-look-at-key-provisions)
- [AI and the NDPA (Mondaq)](https://www.mondaq.com/nigeria/data-protection/1808954/ai-big-data-and-the-ndpa-nigerias-data-protection-rules-are-about-to-get-stricter)
- [NDPA and GAID 2025 guide (LawPavilion)](https://blog.lawpavilion.com/ndp-act-2023-gaid-2025-a-comprehensive-guide-to-nigerias-new-data-protection-landscape)
