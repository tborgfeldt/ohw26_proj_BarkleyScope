# ohw26_proj_BarkleyScope

Template for the BarkleyScope Oceanhackweek project repo. 

**Folder Structure**

* `contributor_folders` (optional) Each contributor can make a folder here and 
push their work here during the week. This will allow everyone to see each others work but prevent any merge conflicts. It is good if participants are new to collaborative coding.
* `final_notebooks` When the team develops shared final notebooks, they 
can be shared here. Make sure to communicate so that you limit merge conflicts.
* `scripts` Shared scripts or functions can be added here.
* `data` Shared dataset can be shared here. Note, do not put large datasets on GitHub. Speak to the organizers if you 
need to share large datasets. Each team member can have a version of the dataset locally in the same folder to 
preserve relative paths, but the dataset does not need to be added to git/GitHub (you can use `.gitignore`).

You can start with a simple structure and as you progress you can refine it to contain more components. [Here](https://cookiecutter-data-science.drivendata.org/#directory-structure) is an example of a more elaborate structure for a data science project.

## Project Name

## One-line Description

## Collaborators

| Name                | Role                |
|---------------------|---------------------|
| Taylor Borgfeldt      | data mining |
| Ben Limer             | data visualization |
| Dwight Owens          | data mining |
| Anais Gentilhomme     | data mining |
| Shannon McClish       | data visualization |
| Carter Burtlake       | floater |


## Planning

* Initial idea: "short description"
* Ideation Slide: [Add link](https://docs.google.com/presentation/d/1_KLEDpLLvtKpH3awDlZRAiOKuHzbEti4CWmhEykuCG8/edit?slide=id.g3f85357d4e2_2_0#slide=id.g3f85357d4e2_2_0)
* Slack channel: local-knowledge-app
* Final presentation: Add link

## Background

## Goals

## Datasets

`final_notebooks/Glider_Curtain_Plot.ipynb` expects two local data files in the same folder
(not committed to GitHub, per the note above — keep your own local copy):
* `Barkley_Sound_Bathymetry.nc` — GEBCO_2026 bathymetry grid used as the curtain-plot basemap.
  Note: its coverage currently stops ~65 km short of Barkley Sound itself (still an open issue).
* `NE_San_Diego_Trough_Aug_2022.csv` — example CalCOFI CTD cast, used for the 2D profile plot.

With `CONFIG["USE_SAMPLE_DATA"] = True` (the default), the notebook runs standalone on
synthetic data and neither file is required.

## Workflow/Roadmap

## Results/Findings

## Lessons Learned

## References

