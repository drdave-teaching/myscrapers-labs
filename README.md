# craigslist-scraper
Let's see how good the GCP instructions are! Students could just clone this repo and try to go after the final project, but it'd be a mistake. Do it yourself and grind through the customization/enhancements to make it better!

Initially, this ran everything that was on the `myscrapers` repo - but now it has been imporved to:
1) update the fields extracted by the LLM (not just RegEx)
2) materialize the LLM data
3) sends the predictions from the RegEx/ETL DT model from GCS to GitHub (hosted by GitHub Actions, not by GCP!)

Students will take it a step further to use the LLM-materialized data to build a better, interpretable model and will send outputs to their repo for review. You can even scrape something else besides cars for your midterm project (recommended!)
