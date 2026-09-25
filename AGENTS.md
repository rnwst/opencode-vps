## Commit and branch conventions

- Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).
  Write concise, imperative messages and validate that lines are at most
  72 characters, except URLs. Write commit messages using Markdown and use
  backticks (``) to refer to files, variables names, function names, or other
  code elements in particular. In the commit message body, you may describe
  previous behaviour in the past tense, e.g. "Previously, X was done. Do Y
  instead." Feel free to use bullet points in the commit message if appropriate.
  Credit the model in a commit-message footer, so that GitHub recognises it as a
  co-author (adapt for model/provider):
  `Co-authored-by: GPT-5.6 Sol <noreply@openai.com>`
- Put effort into formulating accurate and helpful commit messages. Use plain,
  precise language and avoid jargon. Take extra care to write high-quality
  commit message headers and consider multiple options before settling on the
  one that describes the changes and their purpose most clearly.
- Use [Conventional Branch](https://conventionalbranch.org/#summary) names with
  `feat` or `fix`. Include the issue number when applicable, for example
  `feat/4-add-login-page`. Do not create a branch for every fix or feature
  the user asks to be impelemented. Only do so when it makes sense or when
  explicitly instructed.
