# TODO: new branch with feature name?
# TODO: motivating example of uise case with the new feature

Does this change affect the "philosophy" of the original idea with the class API


Does the use of .to_schema(), for when you acutally know if the format is valid or not, imply that we should be able to add more columns/validations "outside" the class, or modify it after?

The above case does not really make sense from a typing point of view, right?


Should you not want the feedback right away then that the schema is invalid (instead of on calling .to_schema()), or cannot be instantiated?
  => If so, some tests need to change, to not even allow them to be instantiadet. (see the DateTime one)
